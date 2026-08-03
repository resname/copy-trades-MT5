// Dump the plaintext login body + key by hooking the two login-specific
// functions located by hook_login_backtrace.js:
//   0x138ce84 -- login encrypt+frame step (called by the login builder,
//                calls the generic send path). Its input arg should hold the
//                PLAINTEXT login body; another arg may hold the key.
//   0x39301c3 -- login-packet builder (assembles login id / password / server
//                / client version, hands the plaintext body to 0x138ce84).
//
// On each entry we dump the four Windows x64 fastcall arg registers
// (rcx, rdx, r8, r9) plus DUMP bytes at each, as hex, inside the payload
// (JSON string, not the send() data arg, so the runner preserves it). A
// lightweight WSASend marker log is kept so we can correlate which
// 0x138ce84 call corresponds to the 0x0c login send.
//
// Frida 17.16.4 API: ptr.readByteArray (instance), Process.findModuleByName +
// mod.base.add(rva), this.context for registers, no `hexdump` name.

var DUMP = 512;

var CIPHER_FUNCS = [
  { rva: 0x138ce84, name: "login_encrypt_frame", len: DUMP },
  { rva: 0x39301c3, name: "login_builder", len: DUMP },
];

function hexOf(p, n) {
  try {
    var b = p.readByteArray(n);
    return Array.from(new Uint8Array(b)).map(function (x) {
      return ('0' + x.toString(16)).slice(-2);
    }).join('');
  } catch (e) { return null; }
}

function asciiOf(hex, n) {
  if (!hex) return null;
  var out = "";
  var bytes = hex.substr(0, n * 2).match(/.{2}/g) || [];
  for (var i = 0; i < bytes.length; i++) {
    var c = parseInt(bytes[i], 16);
    out += (c >= 0x20 && c < 0x7f) ? String.fromCharCode(c) : ".";
  }
  return out;
}

function dumpArgs(ctx, name, len) {
  var n = len || DUMP;
  var regs = [ctx.rcx, ctx.rdx, ctx.r8, ctx.r9];
  var args = regs.map(function (a, i) {
    var ptr = a.toString();
    var hex = null, ascii = null;
    try { hex = hexOf(a, n); ascii = asciiOf(hex, 96); } catch (e) {}
    return { i: i, ptr: ptr, hex: hex, ascii: ascii };
  });
  send({ type: "cipher_hit", name: name + ":in", ts: new Date().toISOString(), args: args });
}

function installCipherHooks() {
  var mod = Process.findModuleByName("terminal64.exe");
  if (!mod) { setTimeout(installCipherHooks, 100); return; }
  CIPHER_FUNCS.forEach(function (c) {
    var addr = mod.base.add(c.rva);
    try {
      Interceptor.attach(addr, {
        onEnter: function (args) { dumpArgs(this.context, c.name, c.len); },
        onLeave: function (retval) {
          send({ type: "cipher_hit", name: c.name + ":out", ts: new Date().toISOString(),
                 ret: retval.toString() });
        }
      });
      console.log("[*] hooked " + c.name + " @ " + addr + " (RVA 0x" + c.rva.toString(16) + ")");
    } catch (e) {
      console.log("[!] failed to hook " + c.name + ": " + e);
    }
  });
}

// Lightweight WSASend marker log for correlation (only login-protocol markers).
var WSABUF_BUF_OFF = Process.pointerSize === 8 ? 8 : 4;
var LOGIN_MARKERS = { 0x00: 1, 0x01: 1, 0x0c: 1, 0x65: 1, 0x66: 1, 0x69: 1, 0x6a: 1 };

function installSockHook() {
  var ws2 = Process.findModuleByName("ws2_32.dll") || Process.findModuleByName("WS2_32.dll");
  if (!ws2) { setTimeout(installSockHook, 100); return; }
  var wsaSend = ws2.findExportByName("WSASend");
  if (!wsaSend) return;
  Interceptor.attach(wsaSend, {
    onEnter: function (args) {
      try {
        var len = args[1].add(0).readU32();
        var buf = args[1].add(WSABUF_BUF_OFF).readPointer();
        if (len <= 0 || buf.isNull()) return;
        var hx = hexOf(buf, Math.min(len, 16));
        if (!hx || hx.length < 2) return;
        var m = parseInt(hx.substr(0, 2), 16);
        if (LOGIN_MARKERS[m] === 1) {
          send({ type: "sock_marker", ts: new Date().toISOString(), marker: m, len: len });
        }
      } catch (e) {}
    }
  });
  console.log("[*] WSASend marker hook installed");
}

installCipherHooks();
installSockHook();
console.log("[*] login plaintext hooks installed");