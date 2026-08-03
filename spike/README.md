# MT5 Protocol RE Spike

Goal: log in to the ava demo MT5 server from Python with no terminal running.

1. `pip install -r requirements.txt`
2. Task 2: capture a login with Wireshark.
3. Task 3-4: hook the terminal with Frida.
4. Tasks 5-6: SKIPPED — the Task 4 stop-condition fired (the trade-server
   login cipher is a custom software implementation with no hooked-crypto
   surface; no plaintext login packet was recovered). See verdict.md.
   `mt5_proto_spike.py` was never built.

Demo accounts only. Capture artifacts are gitignored.
