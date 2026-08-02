"""Parse a login pcap and print the MT5 server host/port and transport verdict.

This script wraps tshark (the Wireshark CLI) to:
  1. List TCP conversations in the capture.
  2. Detect a TLS ClientHello on common MT5 ports (443/444/5500+).
  3. Print a TRANSPORT_VERDICT line: either `TLS` (if a ClientHello was seen)
     or `custom-cipher-or-plain` (no TLS handshake observed).
  4. If TLS, print the destinations (ip + port) of the ClientHello packets.

tshark is located by checking, in order:
  * the `TSHARK` environment variable,
  * `C:\\Program Files\\Wireshark\\tshark.exe` (default Wireshark install on Windows),
  * `tshark` on PATH (lets non-Windows / PATH-configured setups work).

If none of those resolve to an executable, the script exits with a clear error
message and a non-zero status, so the controller / user can fix the environment
before re-running.
"""

import os
import sys
import shutil
import subprocess


# Default Wireshark install location on Windows. Hardcoded as a fallback only;
# the TSHARK env var and PATH are tried first.
_DEFAULT_TSHARK_WIN = r"C:\Program Files\Wireshark\tshark.exe"


def find_tshark():
    """Return a path to a usable tshark executable, or raise SystemExit."""
    candidates = []

    env_tshark = os.environ.get("TSHARK")
    if env_tshark:
        candidates.append(env_tshark)

    # Only consider the Windows default path if the file actually exists, so we
    # don't emit a confusing "no such file" error on Linux/macOS.
    if os.path.isfile(_DEFAULT_TSHARK_WIN):
        candidates.append(_DEFAULT_TSHARK_WIN)

    # Fall back to whatever is on PATH (may be None if not installed).
    path_tshark = shutil.which("tshark")
    if path_tshark:
        candidates.append(path_tshark)

    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand

    raise SystemExit(
        "ERROR: could not locate tshark. Set the TSHARK env var to the full "
        "path of tshark.exe (e.g. C:\\Program Files\\Wireshark\\tshark.exe), "
        "add Wireshark's directory to PATH, or install tshark."
    )


def run_tshark(tshark, args):
    """Run tshark with the given args list and return the completed process."""
    cmd = [tshark] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # tshark returns non-zero on a missing/invalid pcap; surface stderr so the
    # caller can produce a clear message instead of silently empty output.
    return proc


def main(pcap):
    tshark = find_tshark()

    if not os.path.isfile(pcap):
        raise SystemExit(
            f"ERROR: capture file not found: {pcap}\n"
            f"tshark located at: {tshark}"
        )

    # 1. List TCP conversations.
    conv = run_tshark(tshark, ["-r", pcap, "-q", "-z", "conv,tcp"])
    if conv.returncode != 0:
        # Most likely an unreadable / non-pcap file.
        raise SystemExit(
            f"ERROR: tshark failed to read {pcap} (rc={conv.returncode}):\n"
            f"{conv.stderr.strip()}"
        )
    print(conv.stdout)

    # 2. Look for TLS ClientHello packets.
    tls = run_tshark(
        tshark,
        ["-r", pcap, "-Y", "tls.handshake.type==1",
         "-T", "fields", "-e", "ip.dst", "-e", "tcp.dstport"],
    )
    if tls.returncode != 0:
        # Don't hard-fail the whole run on a filter error, but warn so it's
        # visible. An empty result is still a valid (non-TLS) verdict.
        sys.stderr.write(
            f"WARNING: tshark TLS filter failed (rc={tls.returncode}): "
            f"{tls.stderr.strip()}\n"
        )

    tls_lines = [l for l in tls.stdout.splitlines() if l.strip()]
    verdict = "TLS" if tls_lines else "custom-cipher-or-plain"
    print(f"TRANSPORT_VERDICT={verdict}")
    if tls_lines:
        print("TLS_CLIENTHELLO_DESTINATIONS:")
        for l in tls_lines:
            print("  " + l)


if __name__ == "__main__":
    pcap_arg = sys.argv[1] if len(sys.argv) > 1 else "spike/capture/ava_login.pcapng"
    main(pcap_arg)