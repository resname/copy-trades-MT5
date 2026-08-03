// Locate the MT5 trade-server login cipher by backward call-stack tracing
// from WSASend. Continuation of the spike beyond the Task 4 stop-condition.
//
// Rationale: the Task 4 crypto hooks ruled out Windows CryptoAPI/CNG and
// AES-NI as the login cipher (the only AES-NI encrypt found is SChannel TLS
// for MT5's HTTPS web APIs). The login cipher is a custom software function
// with no hooked-crypto surface. But the encrypted bytes do not appear from
// nowhere -- some function writes them into the WSASend buffer. By hooking
// WSASend and capturing Thread.backtrace on the send that carries the 0x0c
// encrypted login packet to 185.97.161.227:1950, the frame that produced the
// ciphertext is the cipher (or its direct caller). That LOCATES the function,
// which the spike never did.
//
// Empirical precondition (checked on this machine, 2026-08-03): the ava demo
// Config/certificates folder is EMPTY -> extended (client-certificate) auth is
// NOT enabled for ava demo. So the login uses STANDARD auth (account + password
// + a handshake-derived session key), the viable-to-RE branch, not the
// per-install .pfx cert branch.
//
// Frida 17.16.4 API (same constraints as hook_crypto.js / hook_sockets.js):
//   - ptr.readByteArray(len) (NativePointer instance), NOT static Memory.readByteArray.
//   - Process.findModuleByName(mod) then instance mod.findExportByName(fn).
//   - this.context for registers inside onEnter; args[] is the arg array.
//   - No function named `hexdump`.
//
// Output (JSON lines via the runner): for each login-protocol send, a
// {type:"login_send", ts, peer, marker, bodylen, len, hex, backtrace:[...]}
// record where backtrace entries are "moduleName!0xRVA" strings resolved from
// the raw addresses. Non-login sends get a lightweight {type:"sock_out",...}
// record for context. The 0x0c record (~932-byte body) is the one we want: its
// backtrace pinpoints the login cipher caller.

// --- WSASend layout (Windows x64) -----------------------------------------
// NTSTATUS WSASend(SOCKET s, LPWSABUF lpBuffers, DWORD dwBufferCount,
//                  LPDWORD lpNumBytesSent, DWORD dwFlags,
//                  LPWSAOVERLAPPED lpOverlapped, LPWSAOVERLAPPED_COMPLETION_ROUTINE ...);
// WSABUF x64: { ULONG len @0; pad @4; CHAR* buf @8 } stride 16.
// WSABUF x32: { ULONG len @0; CHAR* buf @4 } stride 8.
// The WSASend buffer is filled BEFORE the call -> read on onEnter.
var WSABUF_STRIDE = Process.pointerSize === 8 ? 16 : 8;
var BUF_OFF = Process.pointerSize === 8 ? 8 : 4;

// --- login-protocol client->server markers on the 1950 trade socket -------
// (from spike/NOTES.md §Server). Keepalive markers (0x32/0x11/0x22/...) are
// intentionally excluded so the hook only fires on the handshake/login packets.
var LOGIN_MARKERS = { 0x00: 1, 0x01: 1, 0x0c: 1, 0x65: 1, 0x66: 1, 0x69: 1, 0x6a: 1 };

function hexOf(p, n) {
  try {
    var b = p.readByteArray(n);
    return Array.from(new Uint8Array(b)).map(function (x) {
      return ('0' + x.toString(16)).slice(-2);
    }).join('');
  } catch (e) { return null; }
}

// Resolve an address to "moduleName!0xRVA" for the backtrace.
function addrInfo(a) {
  try {
    var m = Process.findModuleByAddress(a);
    if (m) return m.name + "!0x" + a.sub(m.base).toString(16);
  } catch (e) {}
  return a.toString();
}

function backtrace(ctx) {
  // ACCURATE first; fall back to FUZZY if it yields too few frames.
  var bt = [];
  try { bt = Thread.backtrace(ctx, Backtracer.ACCURATE); } catch (e) { bt = []; }
  if (!bt || bt.length < 4) {
    try { bt = Thread.backtrace(ctx, Backtracer.FUZZY); } catch (e) {}
  }
  return bt.map(addrInfo);
}

// Best-effort peer resolution via getpeername, to CONFIRM the send is to the
// 1950 trade server (185.97.161.227:1950) rather than a coincidental
// first-byte match on some other socket. Failures are non-fatal.
var _getpeername = null;
function peerOf(sock) {
  if (!_getpeername) return null;
  try {
    var name = Memory.alloc(32);          // sockaddr_in6 worst case
    var namelen = Memory.alloc(4);
    namelen.writeU32(32);
    var rc = _getpeername(sock, name, namelen);
    if (rc.toInt32 ? rc.toInt32() : rc) return null; // SOCKET_ERROR
    var family = name.readU16();
    if (family === 2) {                   // AF_INET: port@2 BE, addr@4
      var port = (name.add(2).readU8() << 8) | name.add(3).readU8();
      var ip = name.add(4).readByteArray(4);
      var ipArr = new Uint8Array(ip);
      var ipStr = ipArr[0] + "." + ipArr[1] + "." + ipArr[2] + "." + ipArr[3];
      return ipStr + ":" + port;
    } else if (family === 23) {           // AF_INET6: port@2 BE
      var port6 = (name.add(2).readU8() << 8) | name.add(3).readU8();
      return "[ipv6]:" + port6;
    }
    return "family=" + family;
  } catch (e) { return null; }
}

function markerOf(hex) {
  if (!hex || hex.length < 2) return null;
  return parseInt(hex.substr(0, 2), 16);
}

function bodylenOf(hex) {
  // bytes 1..4 little-endian uint32
  if (!hex || hex.length < 10) return null;
  var b = hex.substr(2, 8);
  return parseInt(b.substr(6, 2) + b.substr(4, 2) + b.substr(2, 2) + b.substr(0, 2), 16);
}

function install() {
  var ws2 = Process.findModuleByName("ws2_32.dll") || Process.findModuleByName("WS2_32.dll");
  if (!ws2) { setTimeout(install, 100); return; }

  try {
    _getpeername = new NativeFunction(ws2.findExportByName("getpeername"), 'int',
                                      ['pointer', 'pointer', 'pointer']);
  } catch (e) { _getpeername = null; }

  var wsaSend = ws2.findExportByName("WSASend");
  var sendFn = ws2.findExportByName("send");

  if (wsaSend) {
    Interceptor.attach(wsaSend, {
      onEnter: function (args) {
        try {
          var sock = args[0];
          var lpBuffers = args[1];
          // read first WSABUF
          var len = lpBuffers.add(0).readU32();
          var buf = lpBuffers.add(BUF_OFF).readPointer();
          if (len <= 0 || buf.isNull()) return;
          var n = Math.min(len, 4096);
          var hx = hexOf(buf, n);
          var m = markerOf(hx);
          if (m !== null && LOGIN_MARKERS[m] === 1) {
            send({
              type: "login_send", ts: new Date().toISOString(),
              via: "WSASend", sock: sock.toString(), peer: peerOf(sock),
              marker: m, bodylen: bodylenOf(hx), len: len,
              hex: hx, backtrace: backtrace(this.context)
            });
          } else {
            send({ type: "sock_out", ts: new Date().toISOString(), via: "WSASend",
                   sock: sock.toString(), peer: peerOf(sock), marker: m, len: len });
          }
        } catch (e) {
          send({ type: "error", where: "WSASend.onEnter", description: String(e) });
        }
      }
    });
    console.log("[*] WSASend hooked @ " + wsaSend);
  }

  if (sendFn) {
    Interceptor.attach(sendFn, {
      onEnter: function (args) {
        try {
          var sock = args[0];
          var buf = args[1];
          var len = args[2].toInt32();
          if (len <= 0 || buf.isNull()) return;
          var hx = hexOf(buf, Math.min(len, 4096));
          var m = markerOf(hx);
          if (m !== null && LOGIN_MARKERS[m] === 1) {
            send({
              type: "login_send", ts: new Date().toISOString(),
              via: "send", sock: sock.toString(), peer: peerOf(sock),
              marker: m, bodylen: bodylenOf(hx), len: len,
              hex: hx, backtrace: backtrace(this.context)
            });
          }
        } catch (e) {
          send({ type: "error", where: "send.onEnter", description: String(e) });
        }
      }
    });
    console.log("[*] send hooked @ " + sendFn);
  }

  if (_getpeername) console.log("[*] getpeername resolved");
  console.log("[*] login backtrace hooks installed (markers: 0x00,0x01,0x0c,0x65,0x66,0x69,0x6a)");
}

install();