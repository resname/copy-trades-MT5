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