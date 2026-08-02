// Hook Winsock send/recv paths and log direction + hex + timestamp.
//
// MT5 networking uses OVERLAPPED WSASend/WSARecv (IOCP-style), not send/recv:
//   - WSASend: the buffer is filled BEFORE the call, so we read + log on onEnter.
//   - WSARecv: the call returns immediately (pending, retval=0) and the buffer is
//     filled later by the I/O completion. Reading in onLeave gives len=0, so we
//     schedule a delayed read (setTimeout 60ms) and re-read the WSABUF.len field
//     that the system updates on completion. This is best-effort for the low-rate
//     handshake: if completion takes longer than 60ms the read may still see len=0
//     (silently skipped), and if the WSABUF is freed before the timer fires the
//     read is wrapped in try/catch and skipped. Good enough for a login spike.
//
// WSABUF layout: { ULONG len; CHAR *buf }. On x64 the struct is 16 bytes due to
// alignment (len at +0, padding, buf at +8); on x32 it is 8 bytes (len at +0,
// buf at +4). Stride = (Process.pointerSize == 8) ? 16 : 8.
//
// Export lookup: Frida 17 deprecates Module.findExportByName(moduleName, exportName).
// We resolve the module via Process.findModuleByName (returns null when not loaded)
// and call the instance method findExportByName(fn) (also returns null if absent).
//
// ws2_32.dll may not be loaded at script-load time, so we retry module resolution +
// hook installation every 100ms via setTimeout until the module is present.
//
// NOTE: do not name the buffer reader `hexdump` — Frida has a built-in global of
// that name; we use `readBuf` to avoid the collision.

var WSABUF_STRIDE = (Process.pointerSize === 8) ? 16 : 8;
var WSA_RECV_DELAY_MS = 60;

function readBuf(buf, len) {
  try {
    // Frida 17 removed the static Memory.readByteArray(ptr, len) helper; use the
    // NativePointer instance method buf.readByteArray(len) instead.
    return buf.readByteArray(Math.min(len, 4096));
  } catch (e) {
    return null;
  }
}

function logEvent(dir, sock, buf, len) {
  if (len <= 0) return;
  var ts = new Date().toISOString();
  var bytes = readBuf(buf, len);
  if (bytes) {
    send({ type: "sock", dir: dir, sock: sock, ts: ts, len: len }, bytes);
  }
}

// Iterate the WSABUF array at `wsaBuf` (count = `count`) and log each segment.
function logWsaBufArray(dir, sock, wsaBuf, count) {
  for (var i = 0; i < count; i++) {
    try {
      var entry = wsaBuf.add(i * WSABUF_STRIDE);
      var len = entry.readU32();                              // WSABUF.len
      var buf = entry.add(Process.pointerSize).readPointer(); // WSABUF.buf
      logEvent(dir, sock, buf, len);
    } catch (e) {
      // skip unreadable segment
    }
  }
}

function installHooks() {
  var ws2Mod = Process.findModuleByName("ws2_32.dll") || Process.findModuleByName("WS2_32.dll");
  if (!ws2Mod) {
    setTimeout(installHooks, 100);
    return;
  }

  ["send", "recv", "WSASend", "WSARecv"].forEach(function (fn) {
    var p = ws2Mod.findExportByName(fn);
    if (!p) { console.log("[!] missing " + fn); return; }
    Interceptor.attach(p, {
      onEnter: function (args) {
        this.fn = fn;
        this.sock = args[0].toInt32();
        if (fn === "send" || fn === "recv") {
          this.dir = (fn === "send") ? "OUT" : "IN";
          this.buf = args[1];
          this.len = args[2].toInt32();
        } else if (fn === "WSASend") {
          // OUT: buffer is ready before the call. Read + log now.
          this.dir = "OUT";
          var wsaBuf = args[1];
          var count = args[2].toInt32();
          logWsaBufArray(this.dir, this.sock, wsaBuf, count);
        } else {
          // WSARecv (IN): overlapped — buffer filled on completion, not now.
          // Store the LPWSABUF + count for a delayed read in onLeave.
          this.dir = "IN";
          this.wsaBuf = args[1];
          this.wsaCount = args[2].toInt32();
        }
      },
      onLeave: function (retval) {
        if (this.fn === "WSASend") {
          // Already logged in onEnter; nothing to do on completion.
          return;
        } else if (this.fn === "WSARecv") {
          // Overlapped: buffer is filled asynchronously after we return. Schedule a
          // delayed read; the system updates WSABUF.len on completion. Best-effort.
          var sock = this.sock;
          var wsaBuf = this.wsaBuf;
          var count = this.wsaCount;
          setTimeout(function () {
            logWsaBufArray("IN", sock, wsaBuf, count);
          }, WSA_RECV_DELAY_MS);
        } else {
          // send / recv: for OUT use the requested len, for IN use bytes received.
          logEvent(this.dir, this.sock, this.buf, (this.dir === "IN") ? retval.toInt32() : this.len);
        }
      }
    });
  });
  console.log("[*] socket hooks installed");
}

installHooks();