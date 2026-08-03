"""Attach Frida to a running MetaTrader 5 terminal and log crypto-hook events.

Writes ``spike/capture/frida_crypto.log`` as one JSON object per line
(``json.dumps(message["payload"])`` for each ``send`` message), so that
``correlate.py`` can parse it with ``json.loads``. This differs from
``capture_login.py`` (which writes a meta-line + tab-hex format for socket
buffers) because crypto events carry their payload as a JSON object, not as a
raw byte buffer.

Attach mode is used (MT5 anti-tamper kills spawned terminals). The terminal
must already be running and logged in.

Usage:
    python capture_crypto.py [script] [log]

Positional args (all optional):
    script  path to hook_crypto.js   (default: spike/scripts/hook_crypto.js)
    log     path to output log       (default: spike/capture/frida_crypto.log)
"""
import argparse
import json
import sys
import time
import frida

IMAGE_NAME = "terminal64.exe"


def on_message_factory(log_path):
    def on_message(message, data):
        with open(log_path, "a", encoding="utf-8") as f:
            if message["type"] == "send":
                payload = message["payload"]
                if isinstance(payload, (dict, list)):
                    f.write(json.dumps(payload) + "\n")
                else:
                    # Safety net: payload is a primitive; wrap so each line is JSON.
                    f.write(json.dumps({"type": "crypto", "raw": payload}) + "\n")
            elif message["type"] == "error":
                rec = {
                    "type": "error",
                    "description": message.get("description"),
                    "stack": message.get("stack"),
                }
                f.write(json.dumps(rec) + "\n")
            else:
                f.write(json.dumps({"type": "unknown", "message": message}) + "\n")
    return on_message


def main():
    parser = argparse.ArgumentParser(description="Frida crypto hook capture for MT5.")
    parser.add_argument("script", nargs="?", default="spike/scripts/hook_crypto.js",
                        help="path to hook_crypto.js")
    parser.add_argument("log", nargs="?", default="spike/capture/frida_crypto.log",
                        help="path to output log")
    args = parser.parse_args()

    try:
        session = frida.attach(IMAGE_NAME)
    except frida.ProcessNotFoundError:
        print(f"[!] no running '{IMAGE_NAME}' found. Launch the ava terminal first, "
              f"then re-run this script.", flush=True)
        sys.exit(1)

    with open(args.script, "r", encoding="utf-8") as fh:
        script = session.create_script(fh.read())
    script.on("message", on_message_factory(args.log))
    script.load()
    print(f"[+] attached to {IMAGE_NAME}, logging to {args.log}. Ctrl+C to stop.",
          flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[+] stopping", flush=True)
    finally:
        try:
            script.unload()
        except Exception:
            pass
        try:
            session.detach()
        except Exception:
            pass


if __name__ == "__main__":
    main()