# MT5 Protocol Notes (ava demo)

## Server
- primary trade server host: 185.97.161.227
- primary trade server port: 1950
- primary transport: custom-cipher (NOT TLS)
- secondary host: 160.79.104.10
- secondary port: 443
- secondary transport: mixed — one custom-cipher connection (client first byte 0x00, "encrypted mode" probe) + one TLS 1.2 connection (records begin 17 03 03); likely the access/control server + web/market data.
- TLS key log obtainable: n/a for the custom-cipher trade server (it is not TLS). The TLS 1.2 connection to 160.79.104.10 is web/market data, not the login path.
- packet framing observed on 1950: first byte 0x32 marker, then uint32 LE body length, then body, then 4-byte trailer/MAC. Server speaks first. Bodies are high-entropy (encrypted).
- framing confirmed from the client side via Frida (capture/frida_sockets.log, sock=3892 custom-cipher login flow): every packet is `marker(1) + bodylen(4 LE) + body(bodylen) + trailer(4)`. Markers observed on the login socket (client→server): 0x00 (client hello / handshake start), 0x01, 0x0c (large ~932-byte body — the encrypted login packet), 0x65/0x66/0x69/0x6a (post-login commands, carry a 2-byte LE sequence counter at body offset 2-3). Small 9-byte keepalives on the persistent trade socket use the same framing with bodylen=0 and a 2-byte LE counter + `02 00` in the trailer slot (e.g. `32 22 00 00 00 f1 00 02 00`). The 0x32 keepalive marker varies (0x32/0x11/0x22/0x23/0x1f/0x33/0x0a) — likely a server-side command/type byte, not a constant.
- IN (server→client) bytes were captured with Frida for the low-rate handshake (the delayed WSARecv read works there). For high-rate/long-lived IN traffic the overlapped WSABUF is recycled by the IOCP pool before the 60 ms timer fires (reads return freed-memory garbage), so Wireshark (capture/ava_login.pcapng) remains the authoritative source for bulk server→client ciphertext.

## Handshake
- mode/protocol byte: not recovered. Client→server first byte on the 1950 socket
  is marker 0x00 (client hello / handshake start), followed by 0x01 and the large
  0x0c login packet (~932-byte body) — see §Server framing. The bytes inside the
  bodies are high-entropy (encrypted) and were NOT decrypted.
- step-by-step: NOT recovered. Only the wire framing (marker + bodylen + body +
  trailer) is known. The handshake contents are encrypted with an unlocated
  cipher (see §Crypto), so the step-by-step could not be reconstructed.

## Login packet
- field layout (offset, size, type, meaning): NOT recovered — the 0x0c packet
  body is encrypted and was not decrypted. Plaintext field offsets unknown.
- password hash algorithm: NOT recovered. No Windows CryptoAPI hash call
  (BCryptHashData fired 139× but ONLY for SChannel TLS handshakes to the web
  APIs — see §Crypto; none correlated with the 1950 trade socket) was observed
  on the login path. If MT5 hashes the password before sending, it does so
  inside unlocated custom code.
- client version string: NOT recovered from the login packet. (The cleartext
  HTTP strings "POST /api/signals/list HTTP/1.1\r\nHost: api.cdnfx.net" etc.
  captured by the AES-NI hook are MT5 web-API HTTPS traffic, NOT the login
  packet — see §Crypto.)

## Account-info response
- field layout (offset, size, type, meaning): NOT recovered. Server→client
  account-info responses ride the same custom-cipher on 1950 and were not
  decrypted. Bulk IN ciphertext is in capture/ava_login.pcapng (Wireshark),
  authoritative but encrypted.

## Crypto
- algorithm: UNKNOWN for the trade-server login. The login cipher on
  185.97.161.227:1950 is a self-contained software implementation in
  terminal64.exe that uses NEITHER Windows CryptoAPI/CNG NOR the AES-NI
  instructions present in the binary. What WAS characterized is SChannel's
  TLS stack (used for MT5's auxiliary HTTPS web APIs), which is NOT the login
  cipher. Evidence (capture/frida_crypto.log, 316 crypto events, 0 errors):
    * cand_aes_enc_0 (RVA 0x1a5a350, 8 calls) — every call's input buffer
      begins with TLS record header 17 03 03 (TLS 1.2 application data). The
      plaintext is HTTP to api.cdnfx.net ("/api/signals/list",
      "/api/users/status") and www.mql5.com ("/api/vhost/hostservers/top").
      So cand_aes_enc_0 is SChannel's TLS AES encrypt, not the login cipher.
    * BCryptEncrypt / BCryptDecrypt: 0 calls. CryptEncrypt / CryptDecrypt: 0
      calls. The login does not go through the Windows symmetric-encrypt API.
    * BCryptHashData (139 calls) + BCryptImportKeyPair (11 calls) — all
      SChannel TLS: ImportKeyPair arg2 is the CNG blob-type string
      ("RSA\0PUBLOB"/"ECCPUBLOB"/"DSSPUBLOB" as UTF-16LE) for importing TLS
      server certificate public keys; HashData is TLS handshake/PRF hashing.
      None correlated (by timestamp or socket) with the 1950 trade socket.
    * The 126 AESENC / 9 AESENCLAST opcode sites in the image are SChannel's
      TLS AES (the forward S-box is absent because AES-NI needs no table); they
      are NOT a separate MT5 trade cipher. The inverse S-box at RVA 0x241abe0
      and SHA-256 K table at 0x241e180 are bundled crypto-library data; the
      SHA-256 LEA site at 0x6c38a4 did not fire during the login capture.
- key exchange: NOT recovered for the trade login. The only key exchange
  observed (BCryptImportKeyPair) was TLS server-cert key import for HTTPS.
- key material source: UNKNOWN — could not be determined because the login
  cipher function was not located. This is the make-or-break unknown for
  standalone viability and is itself a stop condition (per spec Task 4 Step 9).
- IV/block mode: UNKNOWN for the trade login. TLS web traffic uses SChannel's
  standard TLS 1.2 AEAD/CBC; irrelevant to the login.
- function offsets in terminal64.exe (module + RVA):
    * SChannel TLS AES encrypt (NOT login): terminal64.exe RVA 0x1a5a350
      (cand_aes_enc_0) — 26 AESENC + 2 AESENCLAST inside; encrypts TLS records
      only.
    * terminal64.exe RVA 0x1a58920 (cand_aes_enc_1) — second AES-related fn,
      did not fire during the capture.
    * terminal64.exe RVA 0x6c38a4 (cand_sha256_kload_A) — SHA-256 K-table LEA
      site; did not fire during the login.
    * LOGIN CIPHER FUNCTION: NOT LOCATED. It is not a Windows CryptoAPI call
      and not an AES-NI function. Locating it would require backward
      call-stack tracing from WSASend on the 1950 socket (Frida Stalker /
      backtrace on the send carrying the 0x0c packet) to find the function
      that produced those encrypted bytes, then RE-ing that custom cipher
      and determining its key material source — a substantial further
      investment beyond this spike's one-session budget.

## Spike verdict (Task 4 Step 9 stop-condition)
- Plaintext login packet obtained: NO.
- Login cipher located: NO (it is not Windows CryptoAPI and not AES-NI).
- Key material source determined: NO.
- Handshake requires broker-issued client certificate: not observed on the
  trade socket (the only cert/key imports were TLS for HTTPS web APIs).
- => STOP. Proceed to Task 7 with a FAIL verdict. Recommend the remote-bridge
  fallback architecture (clean GUI on the user's machine; a small headless
  bridge host running the MT5 terminal exposes account state/trade actions
  over a local protocol). The standalone no-terminal login path is not viable
  within the spike's RE budget: the trade login cipher is a custom software
  implementation with no hooked-crypto surface, and even locating it would
  leave the key-material-source question (per-session / per-install /
  broker-issued) unresolved — itself a likely stop condition.

## RE continuation: login-cipher location (WSASend backtrace)
Date: 2026-08-03. Beyond the Task 4 stop-condition, per user direction to
explore the RE further. Script: spike/scripts/hook_login_backtrace.js.
Method: hook WSASend/send, capture Thread.backtrace on the send carrying the
0x0c encrypted login packet. The encrypted bytes do not appear from nowhere --
some function writes them into the WSASend buffer; the backtrace locates it.

Precondition (empirical, this machine): ava demo Config/certificates/ is EMPTY
-> extended (client-certificate) auth is NOT enabled. Standard auth branch
(account + password + handshake-derived session key), the viable-to-RE one.

Capture (spike/capture/frida_backtrace.log, 40 events: 8 login_send + 32
sock_out, 0 errors). The login this run went to 3.10.134.148:443 (AvaTrade
rotated the server from the earlier 185.97.161.227:1950; same protocol --
identical markers + framing, raw custom-cipher over 443, NOT TLS). Full login
flow captured: 0x00 hello, 0x01, 0x0c (bodylen 932 -- the encrypted login),
0x69, 0x6a, 0x66, 0x65 x2.

0x0c login-packet backtrace (innermost [0] = WSASend caller -> outermost):
  [0]  terminal64.exe!0x139db7d  ┐ generic packet send/framing path
  [1]  terminal64.exe!0x139d8c6  │ (shared with every packet type)
  [2]  terminal64.exe!0x139e6ca  ┘
  [3]  terminal64.exe!0x138ce84  <- LOGIN encrypt+frame step (login-specific)
  [4]  terminal64.exe!0x39301c3  <- LOGIN packet builder (handler range 0x393xxxx)
  [5]  terminal64.exe!0x1390a84  ┐ login command dispatch
  [6]  terminal64.exe!0x139f166  │
  [7]  terminal64.exe!0x139eb95  │
  [8]  terminal64.exe!0x13a4ae3  │
  [9]  terminal64.exe!0x13a4a2d  ┘
  [10] terminal64.exe!0x1b2f98b  (thread/io loop)
  [11] KERNEL32.DLL!0xf170c

Frames [0]-[2] are the generic send path (appear in all 8 packets). The
login-specific frames are [3] 0x138ce84 and [4] 0x39301c3. By call order
(outer calls inner), 0x39301c3 (login builder) calls 0x138ce84 (encrypt+frame)
which calls the generic send path [2]->[1]->[0]. So the cipher is most likely
INSIDE 0x138ce84, with the PLAINTEXT login body as its input arg. 0x39301c3
assembles the login fields (login id / password / server / client version) and
hands the plaintext body to 0x138ce84 to encrypt.

Other packet-specific builder frames (for comparison, each command has its own):
  0x00 hello:  [2] 0x3d77643
  0x01:        [2] 0x392e59f
  0x69:        [3] 0xd52aa0, [4] 0xcb3f7b
  0x65:        [3] 0xd9185e/0xd7cd6e, [4] 0xd918e5, [5] 0xf4a457

Next RE step (proposed): hook 0x138ce84 and 0x39301c3 on entry, dump the four
fastcall arg registers + 256 bytes at each. 0x138ce84's input should contain
the PLAINTEXT login body (and possibly the key); 0x39301c3's inputs should
contain the login fields being assembled. That capture would (a) confirm which
function encrypts, (b) recover the plaintext login packet, and (c) reveal the
key + where it comes from (answering the key-material-source viability
question directly).
