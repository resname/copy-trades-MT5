// Crypto/serialize hooks for MT5 terminal64.exe + reused Winsock socket hooks.
//
// Pairs plaintext (crypto function probes) with ciphertext (wire bytes) on one
// timeline so the manual capture (capture_crypto.py) can correlate them via
// correlate.py. Three coverage layers:
//
//   Layer A (PRIMARY, AES-NI-proof): Windows CryptoAPI/CNG hooks. MT5 uses
//   Windows crypto APIs (the enumeration found the CryptoAPI algorithm-name
//   string "...WithSHA1And128BitRC4"). These functions are EXPORTED by
//   bcrypt.dll / advapi32.dll, so they are hooked by name with no disassembly.
//   BCryptEncrypt/BCryptDecrypt/CryptEncrypt/CryptDecrypt additionally dump
//   their explicit input/output buffers in onLeave -- the highest-value
//   plaintext source, AES-NI-proof because the API boundary abstracts the
//   primitive.
//
//   Layer B: AES-NI function-entry probes in CRYPTO_FUNCS, resolved by
//   opcode-scanning the terminal64.exe image (AESENC/AESENCLAST/AESDEC/
//   AESDECLAST sites) and walking backwards to the function entry via MSVC
//   cc-padding. These hook the AES block function entry; the fastcall args
//   (rcx/rdx/r8) hold the plaintext buffer + key + round keys on entry.
//
//   Layer C: the SHA-256 K-table LEA probe site (mid-function) in CRYPTO_FUNCS.
//
// CRYPTO_FUNCS holds {module, rva, name, len} entries for function entries /
// instruction sites to probe via Interceptor.attach. onEnter dumps the four
// Windows x64 fastcall arg registers (rcx, rdx, r8, r9) plus `len` bytes
// (default 128, AES entries use 256) at each; onLeave dumps the return value.
// RVAs are stable across launches (ASLR only changes the module base).
//
// Frida 17.16.4 API notes (the brief's template was broken by these changes):
//   - Static Memory.readByteArray(ptr, len) is REMOVED. Use ptr.readByteArray(len).
//   - Static Module.findExportByName(mod, fn) is deprecated/removed. Use
//     Process.findModuleByName(mod) (returns null if absent) then the instance
//     method mod.findExportByName(fn) (returns null if absent).
//   - Do NOT name a function `hexdump` (Frida has a built-in global of that name).
//   - Windows x64 fastcall arg registers are ctx.rcx, ctx.rdx, ctx.r8, ctx.r9
//     (the brief's template had a typo `ctx.r cx`).
//   - Interceptor.attach onEnter(args): the parameter is the args array, NOT a
//     CpuContext. The CpuContext is `this.context` inside onEnter. We pass
//     `this.context` to dumpArgs so ctx.rcx/rdx/r8/r9 resolve correctly.
//
// The socket-hook block reuses the proven overlapped-IO logic from
// spike/scripts/hook_sockets.js (read it; do not edit it): WSASend logs on
// onEnter, WSARecv schedules a delayed read (I/O completion fills the buffer
// asynchronously), ws2_32.dll resolution is retried until the module is loaded.

// ---------------------------------------------------------------------------
// CRYPTO_FUNCS: filled from Frida enumeration (Step 3/4). See task-4-report.md.
//   - cand_aes_enc_0: AES-NI encrypt block function. 26 AESENC + 2 AESENCLAST
//     sites inside it = unrolled AES round loop (AES-128/256 encrypt block).
//   - cand_aes_enc_1: second AES encrypt-related function (1 AESENCLAST site).
//   - cand_sha256_kload_A: LEA loading the SHA-256 K-table into r14; enclosing
//     function is a SHA-256 implementation. Mid-function probe site.
// ---------------------------------------------------------------------------
const CRYPTO_FUNCS = [
  { module: "terminal64.exe", rva: 0x1a5a350, name: "cand_aes_enc_0", len: 256 },
  { module: "terminal64.exe", rva: 0x1a58920, name: "cand_aes_enc_1", len: 256 },
  { module: "terminal64.exe", rva: 0x6c38a4,  name: "cand_sha256_kload_A", len: 128 },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function hexOf(p, n) {
  try {
    var b = p.readByteArray(n);
    return Array.from(new Uint8Array(b)).map(function (x) {
      return ('0' + x.toString(16)).slice(-2);
    }).join('');
  } catch (e) {
    return null;
  }
}

// ctx is a CpuContext (this.context from onEnter). Dumps the four fastcall arg
// registers plus `len` bytes at each.
function dumpArgs(ctx, name, len) {
  var n = len || 128;
  var regs = [ctx.rcx, ctx.rdx, ctx.r8, ctx.r9];
  var args = regs.map(function (a, i) {
    return { i: i, ptr: a.toString(), hex: hexOf(a, n) };
  });
  send({ type: "crypto", name: name, ts: new Date().toISOString(), args: args });
}

function dumpRet(name, retval) {
  var rec = { type: "crypto", name: name, ts: new Date().toISOString(), args: [] };
  try {
    rec.args.push({ i: 0, ptr: retval.toString(), hex: hexOf(retval, 128) });
  } catch (e) {
    // retval is a value, not always a dereferenceable pointer; send ptr only.
    try { rec.args.push({ i: 0, ptr: retval.toString(), hex: null }); }
    catch (e2) { rec.args.push({ i: 0, ptr: "?", hex: null }); }
  }
  send(rec);
}

// ---------------------------------------------------------------------------
// Layer B/C: install CRYPTO_FUNCS probes
// ---------------------------------------------------------------------------
CRYPTO_FUNCS.forEach(function (c) {
  var mod = Process.findModuleByName(c.module);
  if (!mod) {
    console.log("[!] module not found: " + c.module);
    return;
  }
  var addr = mod.base.add(c.rva);
  var len = c.len || 128;
  try {
    Interceptor.attach(addr, {
      onEnter: function (args) { dumpArgs(this.context, c.name + ":in", len); },
      onLeave: function (retval) { dumpRet(c.name + ":out", retval); }
    });
    console.log("[*] hooked " + c.name + " @ " + addr + " (RVA 0x" +
      c.rva.toString(16) + ", " + len + "B)");
  } catch (e) {
    console.log("[!] failed to hook " + c.name + " @ " + addr + ": " + e);
  }
});

// ---------------------------------------------------------------------------
// Layer A: Windows CryptoAPI / CNG hooks (PRIMARY, AES-NI-proof, exported).
// bcrypt.dll / advapi32.dll may load late -> retry install every 100ms.
// ---------------------------------------------------------------------------
var WIN_LEN = 128;

// Simple hook: dump fastcall regs on entry + retval on leave.
function simpleWinHook(fn) {
  return {
    onEnter: function (args) { dumpArgs(this.context, "Win:" + fn + ":in", WIN_LEN); },
    onLeave: function (retval) { dumpRet("Win:" + fn + ":out", retval); }
  };
}

// BCryptEncrypt/Decrypt: dump explicit input + output buffers in onLeave.
//   NTSTATUS BCryptEncrypt(hKey rcx, pbInput rdx, cbInput r8, pPaddingInfo r9,
//                          pbIV [sp], cbIV [sp], pbOutput [sp], cbOutput [sp],
//                          pcbOutput [sp], dwFlags [sp])
// Frida's args array indexes all 10 params: args[1]=pbInput, args[2]=cbInput,
// args[6]=pbOutput, args[8]=pcbOutput.
function bcryptCryptHook(fn) {
  return {
    onEnter: function (args) {
      dumpArgs(this.context, "Win:" + fn + ":in", WIN_LEN);
      this.pbInput = args[1];
      this.cbInput = args[2].toInt32();
      this.pbOutput = args[6];
      this.pcbOutput = args[8];
    },
    onLeave: function (retval) {
      dumpRet("Win:" + fn + ":out", retval);
      var produced = -1;
      try { produced = this.pcbOutput.readU32(); } catch (e) {}
      var inHex = (this.cbInput > 0) ? hexOf(this.pbInput, this.cbInput) : null;
      var outHex = (produced > 0) ? hexOf(this.pbOutput, produced) : null;
      send({
        type: "crypto",
        name: "Win:" + fn + ":io",
        ts: new Date().toISOString(),
        args: [
          { i: 0, ptr: this.pbInput.toString(), hex: inHex,
            len: this.cbInput, role: "input" },
          { i: 1, ptr: this.pbOutput.toString(), hex: outHex,
            len: produced, role: "output" }
        ]
      });
    }
  };
}

// CryptEncrypt/Decrypt: pbData is in/out; *pdwDataLen is input len on entry and
// output len on return.
//   BOOL CryptEncrypt(hKey rcx, hHash rdx, Final r8, dwFlags r9,
//                     pbData [sp], pdwDataLen [sp], dwBufLen [sp])
// args[4]=pbData, args[5]=pdwDataLen.
function cryptCryptHook(fn) {
  return {
    onEnter: function (args) {
      dumpArgs(this.context, "Win:" + fn + ":in", WIN_LEN);
      this.pbData = args[4];
      this.pDataLen = args[5];
      this.inLen = -1;
      try { this.inLen = this.pDataLen.readU32(); } catch (e) {}
    },
    onLeave: function (retval) {
      dumpRet("Win:" + fn + ":out", retval);
      var outLen = -1;
      try { outLen = this.pDataLen.readU32(); } catch (e) {}
      var inHex = (this.inLen > 0) ? hexOf(this.pbData, this.inLen) : null;
      var outHex = (outLen > 0) ? hexOf(this.pbData, outLen) : null;
      send({
        type: "crypto",
        name: "Win:" + fn + ":io",
        ts: new Date().toISOString(),
        args: [
          { i: 0, ptr: this.pbData.toString(), hex: inHex,
            len: this.inLen, role: "input" },
          { i: 1, ptr: this.pbData.toString(), hex: outHex,
            len: outLen, role: "output" }
        ]
      });
    }
  };
}

var BCRYPT_SIMPLE = [
  "BCryptHash", "BCryptHashData", "BCryptGenerateSymmetricKey",
  "BCryptImportKey", "BCryptImportKeyPair", "BCryptSecretAgreement",
  "BCryptDeriveKeyCapi", "BCryptDeriveKeyPBKDF2", "BCryptGenerateKeyPair"
];
var ADVAPI32_SIMPLE = [
  "CryptCreateHash", "CryptDeriveKey", "CryptGenKey", "CryptImportKey",
  "CryptDuplicateKey", "CryptSetKeyParam", "CryptHashData"
];

function factoryFor(fn) {
  if (fn === "BCryptEncrypt" || fn === "BCryptDecrypt") return bcryptCryptHook;
  if (fn === "CryptEncrypt" || fn === "CryptDecrypt") return cryptCryptHook;
  return simpleWinHook;
}

function installWinCrypto() {
  var bcrypt = Process.findModuleByName("bcrypt.dll");
  var advapi = Process.findModuleByName("advapi32.dll") ||
    Process.findModuleByName("ADVAPI32.dll");
  if (!bcrypt && !advapi) {
    setTimeout(installWinCrypto, 100);
    return;
  }

  function hook(mod, fn) {
    var p = mod.findExportByName(fn);
    if (!p) { return false; }
    try {
      Interceptor.attach(p, factoryFor(fn)(fn));
      console.log("[*] Win hook " + fn + " @ " + p);
    } catch (e) {
      console.log("[!] Win hook " + fn + " failed: " + e);
    }
    return true;
  }

  if (bcrypt) {
    ["BCryptEncrypt", "BCryptDecrypt"].concat(BCRYPT_SIMPLE).forEach(function (fn) {
      hook(bcrypt, fn);
    });
  }
  if (advapi) {
    ["CryptEncrypt", "CryptDecrypt"].concat(ADVAPI32_SIMPLE).forEach(function (fn) {
      hook(advapi, fn);
    });
  }
  console.log("[*] Windows crypto API hooks installed");
}

installWinCrypto();

// ---------------------------------------------------------------------------
// Reused Winsock socket hooks (mirror of spike/scripts/hook_sockets.js logic).
// Kept inline so plaintext (crypto) and ciphertext (wire) events share one
// timeline in a single script load. ws2_32.dll may load late -> retry install.
// ---------------------------------------------------------------------------
var WSABUF_STRIDE = (Process.pointerSize === 8) ? 16 : 8;
var WSA_RECV_DELAY_MS = 60;

function readBuf(buf, len) {
  try {
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

function logWsaBufArray(dir, sock, wsaBuf, count) {
  for (var i = 0; i < count; i++) {
    try {
      var entry = wsaBuf.add(i * WSABUF_STRIDE);
      var len = entry.readU32();
      var buf = entry.add(Process.pointerSize).readPointer();
      logEvent(dir, sock, buf, len);
    } catch (e) {
      // skip unreadable segment
    }
  }
}

function installSocketHooks() {
  var ws2Mod = Process.findModuleByName("ws2_32.dll") ||
    Process.findModuleByName("WS2_32.dll");
  if (!ws2Mod) {
    setTimeout(installSocketHooks, 100);
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
          this.dir = "OUT";
          var wsaBuf = args[1];
          var count = args[2].toInt32();
          logWsaBufArray(this.dir, this.sock, wsaBuf, count);
        } else {
          this.dir = "IN";
          this.wsaBuf = args[1];
          this.wsaCount = args[2].toInt32();
        }
      },
      onLeave: function (retval) {
        if (this.fn === "WSASend") {
          return;  // already logged in onEnter
        } else if (this.fn === "WSARecv") {
          var sock = this.sock;
          var wsaBuf = this.wsaBuf;
          var count = this.wsaCount;
          setTimeout(function () {
            logWsaBufArray("IN", sock, wsaBuf, count);
          }, WSA_RECV_DELAY_MS);
        } else {
          logEvent(this.dir, this.sock, this.buf,
            (this.dir === "IN") ? retval.toInt32() : this.len);
        }
      }
    });
  });
  console.log("[*] socket hooks installed");
}

installSocketHooks();
console.log("[*] crypto + Windows API + socket hooks installed");