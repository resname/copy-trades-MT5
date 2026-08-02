"""Attach Frida to a running MetaTrader 5 terminal and log socket traffic.

Default mode is ``attach`` (recommended): the terminal must already be running
because MT5 anti-tamper kills the process if it is spawned under Frida. Use
``--mode spawn`` only if you know the spawn path works for your target.

Usage:
    python capture_login.py [--mode attach|spawn] [script] [target] [log]

Positional args (all optional, for backward compatibility):
    script  path to hook_sockets.js      (default: spike/scripts/hook_sockets.js)
    target  path to terminal64.exe       (default: ava demo path; unused in attach mode)
    log     path to output log           (default: spike/capture/frida_sockets.log)
"""
import argparse
import sys
import time
import frida

IMAGE_NAME = "terminal64.exe"


def on_message_factory(log_path):
    def on_message(message, data):
        with open(log_path, "ab") as f:
            if message["type"] == "send":
                m = message["payload"]
                line = f'{m["ts"]}\t{m["dir"]}\tsock={m["sock"]}\tlen={m["len"]}'
                f.write(line.encode() + b"\n")
                if data:
                    f.write(b"\t") ; f.write(data.hex().encode()) ; f.write(b"\n")
            else:
                f.write(b"[frida-error] " + str(message).encode() + b"\n")
    return on_message


def attach_mode(script_path, log_path):
    try:
        session = frida.attach(IMAGE_NAME)
    except frida.ProcessNotFoundError:
        print(f"[!] no running '{IMAGE_NAME}' found. Launch the ava terminal first, "
              f"then re-run this script.", flush=True)
        sys.exit(1)
    with open(script_path, "r", encoding="utf-8") as fh:
        script = session.create_script(fh.read())
    script.on("message", on_message_factory(log_path))
    script.load()
    print(f"[+] attached to {IMAGE_NAME}, logging to {log_path}. Ctrl+C to stop.", flush=True)
    return session, script


def spawn_mode(script_path, target_path, log_path):
    pid = frida.spawn(target_path)
    session = frida.attach(pid)
    with open(script_path, "r", encoding="utf-8") as fh:
        script = session.create_script(fh.read())
    script.on("message", on_message_factory(log_path))
    script.load()
    frida.resume(pid)
    print(f"[+] spawned pid={pid}, logging to {log_path}. Ctrl+C to stop.", flush=True)
    return session, script


def main():
    parser = argparse.ArgumentParser(description="Frida socket hook capture for MT5.")
    parser.add_argument("--mode", choices=["attach", "spawn"], default="attach",
                        help="attach to a running terminal (default) or spawn a new one.")
    parser.add_argument("script", nargs="?", default="spike/scripts/hook_sockets.js",
                        help="path to hook_sockets.js")
    parser.add_argument("target", nargs="?",
                        default=r"C:\Program Files\MetaTrader 5 ava demo\terminal64.exe",
                        help="path to terminal64.exe (spawn mode only)")
    parser.add_argument("log", nargs="?", default="spike/capture/frida_sockets.log",
                        help="path to output log")
    args = parser.parse_args()

    if args.mode == "attach":
        session, script = attach_mode(args.script, args.log)
    else:
        session, script = spawn_mode(args.script, args.target, args.log)

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
