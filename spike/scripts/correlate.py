"""Merge frida_sockets.log + frida_crypto.log into spike/capture/timeline.tsv.

The timeline interleaves SOCK (wire/ciphertext) and CRYPTO (plaintext/function
probe) events sorted by ISO timestamp so the manual capture can be read as one
chronology.

Input formats:
- ``frida_sockets.log``: meta-line then optional tab-prefixed hex line, as
  produced by ``capture_login.py`` / ``hook_sockets.js``. Meta columns are
  ``ts\tdir\tsock=...\tlen=...``.
- ``frida_crypto.log``: one JSON object per line, as produced by
  ``capture_crypto.py``. Each object has ``type`` (``crypto``/``sock``/``error``),
  ``name``/``dir``, ``ts``, and (for crypto) an ``args`` array of
  ``{i, ptr, hex}`` records.

Robust to a missing ``frida_crypto.log`` (the manual crypto capture has not
necessarily been run yet); in that case the timeline is SOCK-only.
"""
import json
import os
import sys

OUT = "spike/capture/timeline.tsv"
SOCK_LOG = "spike/capture/frida_sockets.log"
CRYPTO_LOG = "spike/capture/frida_crypto.log"

rows = []


def parse_sockets(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        meta = lines[i].rstrip("\n")
        hexline = lines[i + 1].rstrip("\n") if i + 1 < len(lines) else ""
        if hexline.startswith("\t"):
            hexdata = hexline.strip()
            consumed = 2
        else:
            hexdata = ""
            hexline = ""
            consumed = 1
        parts = meta.split("\t")
        if parts and parts[0].startswith("20"):
            rows.append((parts[0], "SOCK", "\t".join(parts[1:]), hexdata))
        i += consumed


def _crypto_meta(obj):
    name = obj.get("name", "")
    args = obj.get("args") or []
    arg_summaries = []
    for a in args:
        ptr = a.get("ptr", "")
        hexb = a.get("hex") or ""
        # Truncate long hex to keep the TSV readable; full hex stays in the JSON log.
        if len(hexb) > 64:
            hexb = hexb[:64] + "..."
        arg_summaries.append(f"arg{a.get('i', '?')}@{ptr}={hexb}")
    return name + " " + " ".join(arg_summaries)


def parse_crypto(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("[frida"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                # Plain text fallback (shouldn't happen with capture_crypto.py).
                rows.append(("", "CRYPTO", line, ""))
                continue
            t = obj.get("type", "")
            if t == "error":
                rows.append(("", "ERROR",
                             obj.get("description", "") + " " + (obj.get("stack") or ""),
                             ""))
                continue
            if t == "sock":
                # hook_crypto.js also relays socket events; normalize to SOCK rows.
                rows.append((obj.get("ts", ""), "SOCK",
                             f"{obj.get('dir', '')}\tsock={obj.get('sock', '')}\tlen={obj.get('len', '')}",
                             ""))
                continue
            # Default: crypto function probe.
            rows.append((obj.get("ts", ""), "CRYPTO", _crypto_meta(obj), ""))


def main():
    parse_sockets(SOCK_LOG)
    parse_crypto(CRYPTO_LOG)
    rows.sort()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("ts\tkind\tmeta\thex\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
    print(f"wrote {len(rows)} rows to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())