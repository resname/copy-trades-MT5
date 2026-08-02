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