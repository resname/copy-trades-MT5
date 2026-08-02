# MT5 Server-Protocol Reverse-Engineering Spike — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove (or disprove) that a Python client can log in to the `ava demo` MT5 server and receive account info with no `terminal64.exe` running.

**Architecture:** Hybrid reverse engineering. Wireshark captures the wire to identify the server and transport shape. Frida hooks `terminal64.exe`'s socket + crypto functions to correlate plaintext commands with ciphertext on the wire. Findings are recorded in `NOTES.md`, then a standalone `mt5_proto_spike.py` reimplements the login handshake and is run cold to hit the viability bar.

**Tech Stack:** Python 3, Frida (`frida-tools`), Wireshark (`tshark`/`dumpcap`), Ghidra (only if crypto needs static confirmation). Target: `ava demo` MT5 terminal on Windows.

## Global Constraints

- Demo accounts only — never capture or log in with a real account.
- The final viability check must run with **no `terminal64.exe` process** running (`tasklist | findstr terminal64.exe` returns empty).
- Capture artifacts (pcaps, Frida logs) can be large and may contain credentials — they are gitignored, never committed.
- `NOTES.md` is the single source of truth for observed protocol values; later tasks read field offsets/crypto details from it by section name.
- Stop conditions from the spec are checked explicitly at the end of Task 4 and Task 6; on a stop, write `verdict.md` (Task 7) and do not continue.
- All work lives under `spike/` in the repo.

---

## File Structure

```
spike/
  README.md                 # how to run the spike
  requirements.txt          # frida-tools
  .gitignore                # ignore pcaps, logs, secrets
  NOTES.md                  # protocol notes: server, handshake, login packet, account-info response, crypto spec
  scripts/
    hook_sockets.js         # Frida: hook send/recv/WSASend/WSARecv, log wire bytes
    hook_crypto.js          # Frida: hook crypto/serialize funcs (offsets from NOTES.md), log plaintext + keys
    capture_login.py        # orchestrates: start dumpcap, launch terminal via Frida, stop capture
    parse_server.py         # parse a pcap, print server host/port + TLS-vs-custom verdict
    correlate.py            # merge Wireshark export + Frida logs into one timeline file
  mt5_proto_spike.py        # standalone cold-login reimplementation
  verdict.md                # pass/fail + evidence + chosen next architecture
  capture/                  # gitignored: *.pcap, *.pcapng, frida_*.log, timeline.tsv
```

Responsibilities:
- `hook_sockets.js` — only network I/O logging. No crypto.
- `hook_crypto.js` — only crypto/serialize interception. Reads function offsets from a config block at the top (filled from `NOTES.md`).
- `capture_login.py` — orchestration only; calls dumpcap and frida, writes files under `capture/`.
- `parse_server.py` — read-only pcap analysis.
- `correlate.py` — read-only merge of exported Wireshark CSV + Frida logs into `capture/timeline.tsv`.
- `NOTES.md` — observed data, structured by fixed section headers so tasks can reference them.
- `mt5_proto_spike.py` — the viability test; no terminal dependency.

---

## Task 1: Spike scaffolding and tooling

**Files:**
- Create: `spike/README.md`
- Create: `spike/requirements.txt`
- Create: `spike/.gitignore`
- Create: `spike/NOTES.md`
- Create: `spike/scripts/.gitkeep`

**Interfaces:**
- Produces: `spike/` directory layout and `NOTES.md` with the fixed section headers used by later tasks.

- [ ] **Step 1: Create the directory structure**

Run:
```bash
mkdir -p spike/scripts spike/capture
```

- [ ] **Step 2: Write `spike/.gitignore`**

```
capture/*.pcap
capture/*.pcapng
capture/*.log
capture/frida_*.log
capture/timeline.tsv
capture/*.csv
secrets.txt
*.key
```

- [ ] **Step 3: Write `spike/requirements.txt`**

```
frida-tools>=12.0.0
```

- [ ] **Step 4: Write `spike/NOTES.md` with fixed section headers**

```markdown
# MT5 Protocol Notes (ava demo)

## Server
- host:
- port:
- transport: (TLS | custom-cipher | plain)
- TLS key log obtainable: (yes | no)

## Handshake
- mode/protocol byte:
- step-by-step:

## Login packet
- field layout (offset, size, type, meaning):
- password hash algorithm:
- client version string:

## Account-info response
- field layout (offset, size, type, meaning):

## Crypto
- algorithm:
- key exchange:
- key material source: (per-session | per-install | hardcoded)
- IV/block mode:
- function offsets in terminal64.exe (module + RVA):
```

- [ ] **Step 5: Write `spike/README.md`**

```markdown
# MT5 Protocol RE Spike

Goal: log in to the ava demo MT5 server from Python with no terminal running.

1. `pip install -r requirements.txt`
2. Task 2: capture a login with Wireshark.
3. Task 3-4: hook the terminal with Frida.
4. Task 5: fill NOTES.md.
5. Task 6: run mt5_proto_spike.py cold.

Demo accounts only. Capture artifacts are gitignored.
```

- [ ] **Step 6: Create `spike/scripts/.gitkeep`** (empty file so the dir is tracked).

- [ ] **Step 7: Verify tooling is installed**

Run:
```bash
python --version
frida --version
tshark --version 2>nul || echo "tshark not found - install Wireshark"
```
Expected: Python 3.x, frida prints a version, tshark prints a version or the Wireshark-install reminder prints.

- [ ] **Step 8: Commit**

```bash
git add spike
git commit -m "feat(spike): scaffold MT5 protocol RE spike"
```

---

## Task 2: Wireshark capture of an ava demo login

**Files:**
- Create: `spike/scripts/parse_server.py`
- Create: `spike/capture/ava_login.pcapng` (gitignored)
- Modify: `spike/NOTES.md` (Server section)

**Interfaces:**
- Consumes: an `ava demo` terminal logged out and ready to log in.
- Produces: `capture/ava_login.pcapng`; `NOTES.md §Server` filled; `parse_server.py` prints host/port + transport verdict.

- [ ] **Step 1: Write `spike/scripts/parse_server.py`**

```python
"""Parse a login pcap and print the MT5 server host/port and transport verdict."""
import sys
import subprocess
import csv
import io

def main(pcap):
    # Use tshark to list TCP conversations.
    out = subprocess.run(
        ["tshark", "-r", pcap, "-q", "-z", "conv,tcp"],
        capture_output=True, text=True
    )
    print(out.stdout)
    # Look for TLS ClientHello on common MT5 ports (443/444/5500+).
    tls = subprocess.run(
        ["tshark", "-r", pcap, "-Y", "tls.handshake.type==1",
         "-T", "fields", "-e", "ip.dst", "-e", "tcp.dstport"],
        capture_output=True, text=True
    )
    tls_lines = [l for l in tls.stdout.splitlines() if l.strip()]
    verdict = "TLS" if tls_lines else "custom-cipher-or-plain"
    print(f"TRANSPORT_VERDICT={verdict}")
    if tls_lines:
        print("TLS_CLIENTHELLO_DESTINATIONS:")
        for l in tls_lines:
            print("  " + l)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "spike/capture/ava_login.pcapng")
```

- [ ] **Step 2: Capture a login**

Manually:
1. Fully close the `ava demo` terminal.
2. Start a capture: `dumpcap -i 1 -w spike/capture/ava_login.pcapng` (adjust interface index; list with `dumpcap -D`).
3. Open the `ava demo` terminal and log in to the demo account.
4. Wait until the account balance is visible, then stop `dumpcap` (Ctrl+C).

- [ ] **Step 3: Verify the capture contains the login**

Run:
```bash
tshark -r spike/capture/ava_login.pcapng -q -z conv,tcp | head -20
```
Expected: at least one TCP conversation to a non-LAN IP on a plausible port (443/444/5500+). If empty, recapture with the correct interface.

- [ ] **Step 4: Run `parse_server.py`**

Run:
```bash
python spike/scripts/parse_server.py spike/capture/ava_login.pcapng > spike/capture/parse_server.out
cat spike/capture/parse_server.out
```
Expected: prints a conversation list and a `TRANSPORT_VERDICT=` line.

- [ ] **Step 5: Fill `NOTES.md §Server`** with the host, port, transport verdict, and whether a TLS key log is obtainable (if TLS, attempt `SSLKEYLOGFILE` env var on a second capture; if no plaintext appears, mark `no`).

- [ ] **Step 6: Commit**

```bash
git add spike/scripts/parse_server.py spike/NOTES.md
git commit -m "feat(spike): wireshark capture of ava demo login + server parse"
```

---

## Task 3: Frida socket hooks and wire-byte timeline

**Files:**
- Create: `spike/scripts/hook_sockets.js`
- Create: `spike/scripts/capture_login.py`
- Create: `spike/capture/frida_sockets.log` (gitignored)

**Interfaces:**
- Produces: `capture/frida_sockets.log` with timestamped hex of every send/recv on the terminal's login socket.

- [ ] **Step 1: Write `spike/scripts/hook_sockets.js`**

```javascript
// Hook Winsock send/recv paths and log direction + hex + timestamp.
const ws2_32 = Module.findBaseAddress("ws2_32.dll") ? "ws2_32.dll" : "WS2_32.dll";

function hexdump(buf, len) {
  try {
    return Memory.readByteArray(buf, Math.min(len, 4096));
  } catch (e) {
    return null;
  }
}

function logEvent(dir, sock, buf, len) {
  if (len <= 0) return;
  const ts = new Date().toISOString();
  const bytes = hexdump(buf, len);
  if (bytes) {
    send({ type: "sock", dir: dir, sock: sock, ts: ts, len: len }, bytes);
  }
}

["send", "recv", "WSASend", "WSARecv"].forEach(function(fn) {
  const p = Module.findExportByName("ws2_32.dll", fn);
  if (!p) { console.log("[!] missing " + fn); return; }
  Interceptor.attach(p, {
    onEnter: function (args) {
      this.fn = fn;
      this.dir = (fn === "send" || fn === "WSASend") ? "OUT" : "IN";
      this.sock = args[0].toInt32();
      if (fn === "send") { this.buf = args[1]; this.len = args[2].toInt32(); }
      else if (fn === "recv") { this.buf = args[1]; this.len = args[2].toInt32(); }
      else if (fn === "WSASend") { this.buf = args[1]; this.len = args[2].toInt32(); }
      else { this.wsaBuf = args[1]; }
    },
    onLeave: function (retval) {
      if (this.fn === "WSARecv") {
        try {
          const len = ptr(this.wsaBuf).readU32(); // WSABUF.len
          const buf = ptr(this.wsaBuf).add(Process.pointerSize).readPointer();
          logEvent(this.dir, this.sock, buf, retval.toInt32());
        } catch (e) {}
      } else {
        logEvent(this.dir, this.sock, this.buf, (this.dir === "IN") ? retval.toInt32() : this.len);
      }
    }
  });
});
console.log("[*] socket hooks installed");
```

- [ ] **Step 2: Write `spike/scripts/capture_login.py`**

```python
"""Launch the ava terminal under Frida with hook_sockets.js and save the log."""
import sys
import time
import frida

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else "spike/scripts/hook_sockets.js"
TARGET = sys.argv[2] if len(sys.argv) > 2 else r"C:\Program Files\MetaTrader 5 ava demo\terminal64.exe"
LOG    = sys.argv[3] if len(sys.argv) > 3 else "spike/capture/frida_sockets.log"

def on_message(message, data):
    with open(LOG, "ab") as f:
        if message["type"] == "send":
            m = message["payload"]
            line = f'{m["ts"]}\t{m["dir"]}\tsock={m["sock"]}\tlen={m["len"]}'
            f.write(line.encode() + b"\n")
            if data:
                f.write(b"\t") ; f.write(data.hex().encode()) ; f.write(b"\n")
        else:
            f.write(b"[frida-error] " + str(message).encode() + b"\n")

pid = frida.spawn(TARGET)
session = frida.attach(pid)
with open(SCRIPT, "r", encoding="utf-8") as fh:
    script = session.create_script(fh.read())
script.on("message", on_message)
script.load()
frida.resume(pid)
print(f"[+] spawned pid={pid}, logging to {LOG}. Ctrl+C to stop.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("[+] stopping")
finally:
    try: session.detach()
    except Exception: pass
```

- [ ] **Step 3: Run a hooked login**

Run:
```bash
python spike/scripts/capture_login.py
```
Then log in to the `ava demo` account in the spawned terminal. After the balance appears, press Ctrl+C.

- [ ] **Step 4: Verify the log captured the login exchange**

Run:
```bash
wc -l spike/capture/frida_sockets.log
grep -c OUT spike/capture/frida_sockets.log
grep -c IN spike/capture/frida_sockets.log
```
Expected: non-zero line count, both OUT and IN present. If empty, confirm Frida attached (check for `[*] socket hooks installed`) and recapture.

- [ ] **Step 5: Commit**

```bash
git add spike/scripts/hook_sockets.js spike/scripts/capture_login.py
git commit -m "feat(spike): frida socket hooks + wire-byte timeline"
```

---

## Task 4: Crypto/serialize hooks and plaintext correlation

**Files:**
- Create: `spike/scripts/hook_crypto.js`
- Create: `spike/scripts/correlate.py`
- Create: `spike/capture/frida_crypto.log` (gitignored)
- Create: `spike/capture/timeline.tsv` (gitignored)
- Modify: `spike/NOTES.md` (Handshake, Login packet, Account-info response, Crypto sections)

**Interfaces:**
- Produces: `capture/timeline.tsv` correlating plaintext, ciphertext, and wire bytes; `NOTES.md` sections Handshake/Login/Account-info/Crypto filled.

- [ ] **Step 1: Stop-condition check before investing in crypto RE**

Run:
```bash
python spike/scripts/parse_server.py spike/capture/ava_login.pcapng | grep TRANSPORT_VERDICT
```
If `TRANSPORT_VERDICT=TLS` AND the `NOTES.md §Server` line `TLS key log obtainable:` is `no`, then attempt one recapture with `SSLKEYLOGFILE` set:
```bash
set SSLKEYLOGFILE=spike/capture/keys.log
python spike/scripts/capture_login.py
```
If TLS plaintext still cannot be recovered, **stop** — this matches the spec's stop condition (TLS + no key log). Skip to Task 7 with a fail verdict.

- [ ] **Step 2: Write `spike/scripts/hook_crypto.js`**

The crypto function offsets are not known yet. This script hooks a configurable list of `{module, rva, name}` entries read from a `CRYPTO_FUNCS` array, plus the Winsock hooks (reused) so plaintext and ciphertext share timestamps. Fill `CRYPTO_FUNCS` in Step 4 from disassembly.

```javascript
// CRYPTO_FUNCS: fill with module + RVA + label from Ghidra/Frida enumeration.
// Example: { module: "terminal64.exe", rva: 0x12340, name: "encrypt_block" }
const CRYPTO_FUNCS = [
  // { module: "terminal64.exe", rva: 0x0, name: "TBD_encrypt" },
  // { module: "terminal64.exe", rva: 0x0, name: "TBD_decrypt" },
];

function dumpArgs(ctx, name) {
  // Log first 4 pointer args + 128 bytes at each pointer on entry.
  const args = [ctx.r cx, ctx.r dx, ctx.r8, ctx.r9]; // x64 fastcall (Windows)
  const out = { name: name, ts: new Date().toISOString(), args: [] };
  args.forEach(function (a, i) {
    out.args.push({ i: i, ptr: a.toString() });
    try { out.args[i].bytes = Memory.readByteArray(a, 128); } catch (e) {}
  });
  send({ type: "crypto", name: name, ts: out.ts }, JSON.stringify(out).concat());
}

CRYPTO_FUNCS.forEach(function (c) {
  const base = Module.findBaseAddress(c.module);
  if (!base) { console.log("[!] module not found: " + c.module); return; }
  const addr = base.add(c.rva);
  Interceptor.attach(addr, {
    onEnter: function (ctx) { dumpArgs(ctx, c.name + ":in"); },
    onLeave: function (retval) {
      send({ type: "crypto", name: c.name + ":out", ts: new Date().toISOString() });
    }
  });
  console.log("[*] hooked " + c.name + " @ " + addr);
});

// Reuse socket logging so crypto and wire events share a timeline.
(function () {
  ["send", "recv", "WSASend", "WSARecv"].forEach(function (fn) {
    const p = Module.findExportByName("ws2_32.dll", fn);
    if (!p) return;
    Interceptor.attach(p, {
      onEnter: function (args) {
        this.fn = fn;
        this.dir = (fn === "send" || fn === "WSASend") ? "OUT" : "IN";
        if (fn === "send") { this.buf = args[1]; this.len = args[2].toInt32(); }
        else if (fn === "recv") { this.buf = args[1]; }
      },
      onLeave: function (retval) {
        try {
          const len = (this.dir === "IN") ? retval.toInt32() : this.len;
          if (len <= 0) return;
          send({ type: "sock", dir: this.dir, ts: new Date().toISOString(), len: len },
               Memory.readByteArray(this.buf, Math.min(len, 4096)));
        } catch (e) {}
      }
    });
  });
})();
console.log("[*] crypto + socket hooks installed");
```

- [ ] **Step 3: Enumerate candidate crypto functions**

Run in a Python REPL / one-off script to list exports and strings hints:
```bash
frida-ps -a | findstr terminal64
python -c "import frida,sys; s=frida.attach('terminal64.exe'); print(s.create_script('''
var m=Module.findBaseAddress(\"terminal64.exe\");
console.log(\"base=\"+m);
Module.enumerateExports(\"terminal64.exe\").forEach(function(e){ if(/crypt|cipher|hash|hmac|aes|rc4|encrypt|decrypt|pack|serial/i.test(e.name)) console.log(e.name+\" @ \"+e.address); });
''').load()); import time; time.sleep(2)"
```
Record any promising export names/RVAs. If exports are sparse, open `terminal64.exe` in Ghidra and search for crypto constants (AES S-box `0x63 0x7c 0x77 0x7b`, RC4 init, SHA-256 K-values `0x428a2f98`) and string references like "encrypt"/"crypt". Note: this is open-ended RE; spend at most one focused session here.

- [ ] **Step 4: Fill `CRYPTO_FUNCS` in `hook_crypto.js`** with the module + RVA + label of the encrypt and decrypt (and serialize, if distinct) functions found in Step 3. Remove the `// TBD` placeholder lines.

- [ ] **Step 5: Run the crypto hook capture**

Run:
```bash
python spike/scripts/capture_login.py spike/scripts/hook_crypto.js "" spike/capture/frida_crypto.log
```
Log in to the demo account, wait for the balance, Ctrl+C.

- [ ] **Step 6: Write `spike/scripts/correlate.py`**

```python
"""Merge frida_sockets.log + frida_crypto.log into capture/timeline.tsv sorted by time."""
import glob, os, json, re

OUT = "spike/capture/timeline.tsv"
rows = []

def parse_sockets(path):
    if not os.path.exists(path): return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        meta = lines[i].rstrip("\n")
        hexline = lines[i+1].rstrip("\n") if i+1 < len(lines) else ""
        if hexline.startswith("\t"):
            hexdata = hexline.strip()
        else:
            hexdata = ""
            hexline = ""
        parts = meta.split("\t")
        if parts and parts[0].startswith("20"):
            rows.append((parts[0], "SOCK", "\t".join(parts[1:]), hexdata))
        i += 2 if hexline.startswith("\t") else 1

def parse_crypto(path):
    if not os.path.exists(path): return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("[frida"): continue
            try:
                obj = json.loads(line)
                rows.append((obj.get("ts",""), "CRYPTO", obj.get("name",""), ""))
            except Exception:
                # fallback: plain text lines
                rows.append(("", "CRYPTO", line, ""))

parse_sockets("spike/capture/frida_sockets.log")
parse_crypto("spike/capture/frida_crypto.log")
rows.sort()
with open(OUT, "w", encoding="utf-8") as f:
    f.write("ts\tkind\tmeta\thex\n")
    for r in rows:
        f.write("\t".join(r) + "\n")
print(f"wrote {len(rows)} rows to {OUT}")
```

- [ ] **Step 7: Build the correlated timeline**

Run:
```bash
python spike/scripts/correlate.py
head -40 spike/capture/timeline.tsv
```
Expected: a TSV with sorted rows showing SOCK and CRYPTO events interleaved. If CRYPTO rows are empty, the offsets in Step 4 are wrong — revisit Step 3 (stop-condition: after one focused session with no plaintext login packet, go to Task 7 fail).

- [ ] **Step 8: Fill `NOTES.md` §Handshake, §Login packet, §Account-info response, §Crypto** from the timeline. Record: the mode/protocol byte, the step-by-step handshake, the login packet field offsets (login id, password hash, server, client version), the account-info response field offsets, the cipher algorithm, key exchange, key material source (per-session/per-install/hardcoded), IV/block mode, and the terminal64.exe function RVAs used.

- [ ] **Step 9: Stop-condition check**

If the key material source is `per-install` and you cannot reproduce the key outside the terminal, OR the handshake requires a broker-issued client certificate, OR no plaintext login packet was obtained, **stop** — go to Task 7 with a fail verdict.

- [ ] **Step 10: Commit**

```bash
git add spike/scripts/hook_crypto.js spike/scripts/correlate.py spike/NOTES.md
git commit -m "feat(spike): crypto hooks + correlated plaintext/ciphertext timeline"
```

---

## Task 5: Protocol notes consolidation and login-packet builder

**Files:**
- Modify: `spike/NOTES.md` (finalize all sections)
- Create: `spike/proto.py`

**Interfaces:**
- Consumes: `NOTES.md` sections filled in Tasks 2 and 4.
- Produces: `spike/proto.py` with `build_login_packet(login, password, server) -> bytes`, `decrypt_response(ciphertext) -> bytes`, and `parse_account_info(plaintext) -> dict`.

- [ ] **Step 1: Re-read `NOTES.md` and confirm every section is filled**

Run:
```bash
grep -c ':$' spike/NOTES.md
```
Manually verify no section still has an empty value. If any are empty, return to Task 4 Step 8 before continuing.

- [ ] **Step 2: Write `spike/proto.py` using the recorded values**

This file implements the crypto and packet assembly from `NOTES.md`. Because the exact algorithm is data-dependent, the structure is fixed and each helper references the NOTES section that supplies its constants.

```python
"""MT5 ava-demo protocol helpers, built from spike/NOTES.md."""
from NOTES_IMPORTS import *  # placeholder removed in Step 3

# === Crypto (from NOTES.md §Crypto) ===
# Fill these from the recorded algorithm/key/IV details.
CIPHER_ALGO = "TODO"   # e.g. "aes-256-cbc" / "rc4" / custom
KEY_SOURCE  = "TODO"   # "per-session" | "per-install" | "hardcoded"
def derive_key(handshake_bytes: bytes) -> bytes:
    """Implement the key derivation recorded in NOTES.md §Crypto."""
    raise NotImplementedError("fill from NOTES.md §Crypto key exchange")

def encrypt(plaintext: bytes, key: bytes) -> bytes:
    raise NotImplementedError("fill from NOTES.md §Crypto algorithm")

def decrypt(ciphertext: bytes, key: bytes) -> bytes:
    raise NotImplementedError("fill from NOTES.md §Crypto algorithm")

# === Login packet (from NOTES.md §Login packet) ===
def build_login_packet(login: int, password: str, server: str) -> bytes:
    """Assemble the login packet using field offsets from NOTES.md §Login packet."""
    raise NotImplementedError("fill from NOTES.md §Login packet")

# === Account-info response (from NOTES.md §Account-info response) ===
def parse_account_info(plaintext: bytes) -> dict:
    """Return {'login': int, 'balance': float} using offsets from NOTES.md §Account-info response."""
    raise NotImplementedError("fill from NOTES.md §Account-info response")
```

- [ ] **Step 3: Remove the placeholder import and implement each function**

Delete `from NOTES_IMPORTS import *`. Implement `derive_key`, `encrypt`, `decrypt`, `build_login_packet`, and `parse_account_info` using the concrete values recorded in `NOTES.md`. Replace each `TODO`/`NotImplementedError` with real code. This is the one task where the code is necessarily derived from observed data — transcribe the offsets and algorithm verbatim from `NOTES.md`.

- [ ] **Step 4: Smoke-test the helpers against captured bytes**

Run:
```bash
python -c "import sys; sys.path.insert(0,'spike'); import proto; print('proto imports OK')"
```
Expected: prints `proto imports OK` with no `NotImplementedError` on import. (Function behavior is exercised in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add spike/proto.py spike/NOTES.md
git commit -m "feat(spike): proto helpers from observed protocol notes"
```

---

## Task 6: Standalone cold-login viability test

**Files:**
- Create: `spike/mt5_proto_spike.py`

**Interfaces:**
- Consumes: `spike/proto.py` (`build_login_packet`, `decrypt_response`, `parse_account_info`) and `NOTES.md §Server`.
- Produces: `mt5_proto_spike.py` that logs in cold and prints account info; the pass/fail evidence for `verdict.md`.

- [ ] **Step 1: Write `spike/mt5_proto_spike.py`**

```python
"""Standalone MT5 ava-demo login. No terminal process may be running."""
import socket
import sys
import subprocess
import time

sys.path.insert(0, ".")
from proto import (derive_key, encrypt, decrypt, build_login_packet, parse_account_info)

# From NOTES.md §Server
HOST = "FILL_HOST"
PORT = 0  # FILL_PORT

def no_terminal_running():
    r = subprocess.run(["tasklist"], capture_output=True, text=True)
    return "terminal64.exe" not in r.stdout

def login_once(login, password, server):
    with socket.create_connection((HOST, PORT), timeout=10) as s:
        # 1. TCP + protocol handshake (bytes from NOTES.md §Handshake)
        s.sendall(b"FILL_HANDSHAKE_INIT")
        hs = s.recv(4096)
        key = derive_key(hs)
        # 2. Login
        pkt = encrypt(build_login_packet(login, password, server), key)
        s.sendall(pkt)
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            resp += chunk
            if len(resp) >= FILL_EXPECTED_RESP_LEN:  # from NOTES.md §Account-info response
                break
        plain = decrypt(resp, key)
        return parse_account_info(plain)

def main():
    if not no_terminal_running():
        print("FAIL: terminal64.exe is running; close it before the cold test.")
        sys.exit(2)
    login = int(sys.argv[1])
    password = sys.argv[2]
    server = sys.argv[3]
    for i in range(2):
        info = login_once(login, password, server)
        print(f"attempt {i+1}: {info}")
        time.sleep(1)

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Fill the `FILL_*` constants** from `NOTES.md` — `HOST`, `PORT` from §Server; `FILL_HANDSHAKE_INIT` and `FILL_EXPECTED_RESP_LEN` from §Handshake / §Account-info response. Remove the placeholder tokens.

- [ ] **Step 3: Close the terminal and run the cold test**

Run:
```bash
tasklist | findstr terminal64.exe   # must be empty
python spike/mt5_proto_spike.py <ava_demo_login> <ava_demo_password> "<ava_demo_server>"
```
Expected (pass): two lines like `attempt 1: {'login': <login>, 'balance': <balance>}` with the real demo balance; exit code 0.
Expected (fail): exception, no response, decrypt garbage, or wrong balance — record the actual output for `verdict.md`.

- [ ] **Step 4: Stop-condition check**

If the run only succeeds by reusing key material captured live from a terminal in the same session (i.e. it does not work cold after a full restart), that is a **partial fail** per the spec — record it honestly and go to Task 7 with a fail verdict.

- [ ] **Step 5: Commit**

```bash
git add spike/mt5_proto_spike.py
git commit -m "feat(spike): standalone cold-login viability test"
```

---

## Task 7: Verdict document and next-architecture decision

**Files:**
- Create: `spike/verdict.md`

**Interfaces:**
- Consumes: the result of Task 6 (pass/fail) plus any stop-condition exits from Tasks 4/6.
- Produces: `verdict.md` stating the verdict, evidence, and the chosen next architecture.

- [ ] **Step 1: Write `spike/verdict.md`**

```markdown
# MT5 Protocol RE Spike — Verdict

Date: (fill)
Target: ava demo

## Result
PASS | FAIL | PARTIAL-FAIL

## Evidence
- Server / transport:
- Crypto key material source:
- Cold login attempts:
  - attempt 1:
  - attempt 2:
- Stop condition hit (if any):

## Conclusion
- (one paragraph: what worked, what didn't, key blockers)

## Next architecture
- On PASS: proceed to a new spec for the standalone copier GUI built on spike/proto.py.
- On FAIL/PARTIAL-FAIL: adopt the remote-bridge architecture (standalone GUI on the user's machine, small headless bridge host with a terminal).
```

- [ ] **Step 2: Fill in the verdict from the Task 6 run** (or from the stop-condition exit). Be honest — if the cold login did not work twice, the result is FAIL/PARTIAL-FAIL.

- [ ] **Step 3: Commit and push**

```bash
git add spike/verdict.md
git commit -m "docs(spike): record MT5 protocol RE spike verdict"
git push origin main
```

- [ ] **Step 4: Report the verdict to the user** and, depending on PASS/FAIL, either start a new brainstorm for the standalone copier GUI or for the remote-bridge architecture.

---

## Self-Review

**Spec coverage:**
- Spike goal (cold login + account info): Tasks 5–6.
- Viability bar (twice in a row, no terminal): Task 6 Steps 3–4.
- Hybrid method (Wireshark + Frida): Tasks 2–4.
- Target ava demo: Task 2 onward.
- Stop conditions (TLS+no keylog, per-install keys, client cert, no plaintext in two sessions): Task 4 Steps 1 & 9, Task 6 Step 4.
- Fallback to remote bridge: Task 7.
- Deliverables (NOTES.md, mt5_proto_spike.py, verdict.md): Tasks 1, 5, 6, 7.
- Demo-only / no-real-account: Global Constraints + Task 2.

**Placeholder scan:** The only `TODO`/`NotImplementedError`/`FILL_*` tokens are in Task 5 Step 2 and Task 6 Step 1, and each is replaced by an explicit follow-up step (Task 5 Step 3, Task 6 Step 2) that transcribes values from `NOTES.md`. These are intentional data-fill points for RE output, not unspecified work. `hook_crypto.js` Step 2 contains commented TBD examples that Step 4 removes.

**Type consistency:** `build_login_packet(login:int, password:str, server:str)->bytes`, `decrypt(ciphertext:bytes, key:bytes)->bytes`, `parse_account_info(plaintext:bytes)->dict` are defined in Task 5 and consumed identically in Task 6. `derive_key(handshake_bytes:bytes)->bytes` is consistent across Tasks 5 and 6.