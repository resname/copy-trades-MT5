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