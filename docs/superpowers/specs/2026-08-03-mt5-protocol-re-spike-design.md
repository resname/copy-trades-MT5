# MT5 Server-Protocol Reverse-Engineering Spike — Design

> Status: spike (research). Decides whether the standalone, no-MT5 trade copier is feasible before any copier code is written.

Date: 2026-08-03

## Background and decision

The project pivoted away from the in-terminal MQL5 Expert Advisor approach. The goal is a standalone GUI program that logs into master and slave MT5 accounts directly, with all copier features (symbol mapping, lot sizing, SL/TP normalization, restart recovery), and **no MetaTrader 5 installation on the computer running the GUI**.

MetaQuotes exposes no client trading API outside the terminal. The official `MetaTrader5` Python package wraps a running `terminal64.exe`. Public reverse-engineering work (e.g. `go-mt5`) covers only the local named-pipe IPC between the Python bridge and the terminal — it still requires the terminal. No public project implements an independent client for the MT5 client→server network protocol.

The user chose to attempt a raw server-protocol reverse-engineering spike before committing to the copier. If the spike fails, the fallback is a remote-bridge architecture: a clean standalone GUI on the user's machine (no MT5) talking to a small headless bridge host that does run a terminal.

## Spike goal

Independently log in to the `ava demo` MT5 server from a Python client with **no `terminal64.exe` running**, and receive valid account info (login + balance) back.

## Verdict criteria

- **Pass →** proceed to design and build the real copier GUI on top of this protocol layer.
- **Fail →** fall back to the remote-bridge architecture. The decision is made on evidence, not optimism.

## Scope

In scope:
- Capturing the `ava demo` login handshake from a real terminal.
- Reverse-engineering the handshake, login packet, and the response carrying account info.
- Reimplementing a minimal independent login in Python.
- Running that reimplementation with no terminal process running.

Out of scope (only if the spike passes):
- Order sending, position/order streaming, market data.
- Symbol mapping, lot sizing, SL/TP normalization, retry/heartbeat.
- The standalone GUI.
- Multi-broker support beyond `ava demo`.

## Viability bar

`mt5_proto_spike.py` logs in cold (no terminal running) and prints real account info (login + balance) **twice in a row** against the `ava demo` server.

A weaker outcome — e.g. it only works by replaying key material captured live from a terminal session — is a **partial fail** and is recorded honestly rather than declared a pass.

## Environment and tooling

- Windows machine with the existing `ava demo` MT5 terminal install. This machine is the observation target only; the final program will not require it.
- Tools to install: Wireshark, Frida (`pip install frida-tools`), Python 3 (already present), Ghidra (only if static confirmation of the cipher is needed).
- A spare demo account on `ava` used only for capture. Demo only, never a real account.

## Method: hybrid (Wireshark + Frida)

### Capture procedure

1. **Wireshark capture.** Start a capture on the outbound adapter, perform a fresh login in the `ava` terminal, stop the capture. Identify:
   - Server host(s) and port(s).
   - Whether traffic is TLS (and whether a key log is obtainable) or a custom cipher (MT4 used a custom scheme on port 443, not SSL).
   - Rough packet shape and sizes.
2. **Frida hooks on `terminal64.exe`:**
   - Hook Windows socket APIs (`WSASend`, `WSARecv`, `send`, `recv`) to log raw wire bytes with timestamps.
   - Hook candidate crypto/serialize functions (located by disassembly) to log plaintext and any key material.
   - Produce one correlated timeline: `[plaintext command] → [ciphertext] → [on wire] → [response ciphertext] → [plaintext response]`.
3. Repeat the login several times to separate the deterministic parts of the handshake from per-session randomness.

### Protocol model and analysis

- Reconstruct from the correlated timeline: the TCP handshake, the initial mode/protocol byte, the login packet field layout (login id, password hash, server, client version), and the response carrying account info.
- Identify the cipher: algorithm, key exchange, IV/block mode. Hooks yield key material and function entry points; Ghidra confirms the algorithm if needed.
- Record findings in a protocol notes file: command table, field offsets, crypto spec. This file becomes the foundation for the real client if the spike passes.

### Standalone reimplementation

- `mt5_proto_spike.py`: with no terminal running, opens TCP to the `ava` server, performs the handshake, sends a reconstructed login packet, decrypts the response, and prints account info.
- Verified on a machine with no MT5 process (same box after fully closing the terminal, or a second clean box).

## Stop conditions

Stop early and declare not viable if any of these are true:
- The protocol is TLS with certificate pinning we cannot bypass.
- The cipher is keyed to per-install hardware or per-install keys we cannot reproduce.
- The handshake requires a broker-issued client certificate.
- After two focused sessions we still cannot extract a plaintext login packet from the hooks.

## Risks and fallback

- **High failure risk.** MT5's wire protocol has no public RE for a reason. The spike is time-boxed; on failure, fall back to the remote-bridge architecture, which still delivers a clean standalone GUI and keeps MT5 off the user's daily machine (a small bridge host runs the terminal).
- **TOS.** Capturing traffic of one's own demo account is research on own credentials but likely violates MetaQuotes/broker TOS. The user accepts this risk. Demo accounts only.
- **Anti-tamper.** If the terminal detects Frida and refuses to run, fall back to Wireshark + static disassembly (method B) or switch to a different hooking tool such as x64dbg.

## Deliverables

- A protocol notes file with the reconstructed handshake, login packet, and crypto spec (partial even on failure, to justify the verdict).
- `mt5_proto_spike.py` (on pass).
- A short verdict document recording the evidence and the pass/fail decision, and the chosen next architecture.

## What comes after a pass

A separate brainstorm/spec cycle for the standalone copier GUI, built on the protocol layer proven by this spike. That cycle covers the GUI (login pages for master and slave, symbol mapping, lot sizing, SL/TP normalization, status/latency, restart recovery), the master→slave diffing and execution engine, and persistence.