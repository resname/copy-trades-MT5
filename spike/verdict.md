# MT5 Protocol RE Spike — Verdict

Date: 2026-08-03
Target: ava demo

## Result

FAIL

## Evidence

- Server / transport:
  - Primary trade server `185.97.161.227:1950`, custom-cipher (NOT TLS). Packet
    framing confirmed from both Wireshark and Frida client-side:
    `marker(1) + bodylen(4 LE) + body(bodylen) + trailer(4)`. Server speaks first.
  - Login socket (Task 3, sock=3892) client→server markers: `0x00` (client hello),
    `0x01`, `0x0c` (large ~932-byte encrypted login packet), then `0x65/0x66/0x69/0x6a`
    post-login commands (2-byte LE sequence counter at body offset 2-3).
  - Secondary `160.79.104.10:443` is mixed: one custom-cipher connection + one TLS 1.2
    connection (web/market data), NOT the login path.
  - Bulk server→client ciphertext is in `spike/capture/ava_login.pcapng` (Wireshark,
    gitignored); authoritative but encrypted.

- Crypto key material source: UNKNOWN — could not be determined, because the
  trade-server login cipher function was never located. This is the make-or-break
  unknown for standalone viability and is itself a stop condition.

- Cold login attempts: NOT ATTEMPTED. Tasks 5 (login-packet builder) and 6
  (standalone cold-login test) were skipped because the Task 4 stop-condition
  fired before a login packet could be constructed. There is no proto.py, no
  derive_key/encrypt/decrypt, and therefore no cold login to report. Recording
  this honestly: the viability bar (independent cold login + account info twice
  in a row, no terminal) was never reached, because the prerequisite — knowing
  the login cipher and key material — was not met.

  - attempt 1: n/a (skipped)
  - attempt 2: n/a (skipped)

- Stop condition hit: YES — Task 4 Step 9. The crypto-hook capture
  (`spike/capture/frida_crypto.log`, 316 crypto events, 0 errors) showed that the
  trade-server login cipher uses NEITHER Windows CryptoAPI/CNG NOR the AES-NI
  instructions present in the binary:
    * `cand_aes_enc_0` (terminal64.exe RVA 0x1a5a350, 8 calls) — every call's
      input begins with TLS record header `17 03 03`; the plaintext is HTTPS to
      `api.cdnfx.net` (`/api/signals/list`, `/api/users/status`) and
      `www.mql5.com` (`/api/vhost/hostservers/top`). This is SChannel's TLS AES
      encrypt for MT5's auxiliary web APIs, NOT the trade login cipher.
    * `BCryptEncrypt` / `BCryptDecrypt`: 0 calls. `CryptEncrypt` / `CryptDecrypt`:
      0 calls. The login does not go through the Windows symmetric-encrypt API.
    * `BCryptImportKeyPair` (11 calls) imports TLS server certificate public keys
      (CNG blob types "RSA PUBLOB"/"ECCPUBLOB"/"DSSPUBLOB" as UTF-16LE);
      `BCryptHashData` (139 calls) is TLS handshake/PRF hashing. None of these
      correlated (by timestamp or socket) with the 1950 trade socket.
    * The 126 AESENC / 9 AESENCLAST opcode sites in terminal64.exe are SChannel's
      TLS AES (forward S-box absent because AES-NI needs no table). The inverse
      S-box at RVA 0x241abe0 and SHA-256 K-table at 0x241e180 are bundled
      crypto-library data; the SHA-256 LEA site at 0x6c38a4 did not fire during
      the login capture.
  - The login cipher is therefore a self-contained custom software implementation
    in the stripped/packed terminal64.exe, with no hooked-crypto surface. No
    plaintext login packet was obtained. Per spec Task 4 Step 9, this is a stop.

## Conclusion

The hybrid Wireshark + Frida method successfully characterized the transport and
framing of the MT5 trade protocol (server address, port, custom-cipher-not-TLS,
packet framing, login-flow markers) and ruled out the obvious crypto surfaces:
the login does NOT use Windows CryptoAPI/CNG and does NOT use the AES-NI
instructions in the binary — the only AES-NI encrypt function found is SChannel's
TLS, which carries MT5's auxiliary HTTPS web traffic, not the login. What the
spike could NOT do is locate the actual trade-server login cipher. It is a custom
software implementation with no hooked-crypto surface, and locating it would
require backward call-stack tracing from WSASend on the 1950 socket (Frida Stalker
/ backtrace on the send carrying the 0x0c packet) to find the function that
produces those encrypted bytes, then RE-ing that custom cipher, then determining
its key material source (per-session / per-install / broker-issued) — itself a
likely further stop condition. That is a substantial investment beyond the spike's
one-session-per-step budget, against a viability bar the spec set deliberately
high (cold login twice, no terminal). The spike did its job: it produced evidence,
not optimism, and the evidence says the standalone no-terminal login path is not
viable within the budget.

## Next architecture

Adopt the **remote-bridge architecture** (FAIL fallback per spec):

- A clean standalone GUI on the user's daily machine — login pages for master and
  slave MT5 accounts, symbol mapping, lot sizing, SL/TP normalization, and all
  the trade-copier features — with **no MT5 terminal installed** on that machine.
- A small headless **bridge host** (a cheap VPS, a spare mini-PC, or a background
  Windows session) that **does** run the MT5 terminal(s). The bridge exposes
  account state and trade actions over a local protocol (a small TCP/HTTP service
  the GUI talks to). Master-side bridges publish position snapshots; slave-side
  bridges receive copy commands and execute them through the terminal.
- This keeps MT5 off the user's daily machine, still delivers the full standalone
  GUI experience, and is buildable on known, supported primitives (the terminal's
  own MQL5 API / IPC) instead of an un-RE'd wire protocol.
- Next step: a new brainstorm + spec for the remote-bridge standalone copier GUI
  (bridge protocol, transport, security, GUI scope), then a writing-plans pass.