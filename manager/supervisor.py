from __future__ import annotations

import multiprocessing
import threading
import time
from dataclasses import dataclass

from manager.engine.copy_loop import CopyEngine
from manager.engine.models import Snapshot
from manager.ipc.messages import (
    AckMsg, StatusMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, ErrorMsg, StartMsg,
)
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.mt5_worker import worker_main


@dataclass
class WorkerHandle:
    name: str
    role: str
    proc: multiprocessing.Process
    pipe: object
    config: dict
    password: str
    adapter_kind: str
    fake_state: dict | None
    last_msg_ts: float = 0.0
    fail_count: int = 0


class Supervisor:
    """Owns worker subprocesses + pipes; routes IPC to/from the CopyEngine;
    watches health and restarts on death. tick() = one slave drain + one timed
    master poll + a health pass. _run() loops it in a daemon thread."""

    def __init__(self, engine: CopyEngine, heartbeat_seconds: int = 5,
                 stale_seconds: float = 30.0, consecutive_failures: int = 3,
                 poll_timeout: float = 0.2, time_fn=time.time,
                 kill_terminal=None):
        self._engine = engine
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_seconds = stale_seconds
        self._consecutive_failures = consecutive_failures
        self._poll_timeout = poll_timeout
        self._time_fn = time_fn
        self._kill_terminal = kill_terminal  # callable(path) or None (Plan 3)
        self._handles: dict[str, WorkerHandle] = {}
        self._last_snapshot_ts = time_fn()
        self.heartbeat_warning = False
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = None
        self.on_restart = None  # callback(name, role) for GUI status (Plan 4)

    def spawn_master(self, config, password, adapter_kind="real", fake_state=None):
        self._handles["master"] = self._spawn("master", "master", config,
                                              password, adapter_kind, fake_state)

    def spawn_slave(self, slave_id, config, password, adapter_kind="real",
                    fake_state=None):
        self._handles[slave_id] = self._spawn(slave_id, "slave", config,
                                              password, adapter_kind, fake_state)

    def _spawn(self, name, role, config, password, adapter_kind, fake_state):
        parent_pipe, child_pipe = multiprocessing.Pipe(duplex=True)
        proc = multiprocessing.Process(target=worker_main,
            args=(child_pipe, role, adapter_kind, fake_state), daemon=True)
        proc.start()
        child_pipe.close()  # parent owns only the parent end
        send_msg(parent_pipe, StartMsg(config=config, password=password))
        return WorkerHandle(name=name, role=role, proc=proc, pipe=parent_pipe,
                            config=config, password=password,
                            adapter_kind=adapter_kind, fake_state=fake_state,
                            last_msg_ts=self._time_fn())

    def tick(self, timeout=None) -> bool:
        to = self._poll_timeout if timeout is None else timeout
        self._drain_slaves()
        ok = self._read_master(to)
        self._health_check()
        return ok

    def _drain_slaves(self) -> None:
        for name, h in list(self._handles.items()):
            if h.role != "slave":
                continue
            while h.pipe is not None and h.pipe.poll(0):
                try:
                    msg = recv_msg(h.pipe)
                except EOFError:
                    self.errors.append(f"slave {name} pipe closed")
                    h.pipe = None
                    break
                self._dispatch_slave(name, msg)
                h.last_msg_ts = self._time_fn()
                h.fail_count = 0

    def _dispatch_slave(self, slave_id, msg) -> None:
        if isinstance(msg, AckMsg):
            for cmd in self._engine.apply_ack(slave_id, msg):
                self._send(slave_id, cmd)
        elif isinstance(msg, StatusMsg):
            self._engine.apply_status(slave_id, msg)
        elif isinstance(msg, RecoveryMsg):
            self._engine.apply_recovery(slave_id, msg.records)
        elif isinstance(msg, SymbolInfoMsg):
            self._engine.apply_symbol_info(slave_id, msg.infos)
        elif isinstance(msg, ErrorMsg):
            self.errors.append(f"{slave_id}: {msg.message}")

    def _read_master(self, timeout) -> bool:
        h = self._handles.get("master")
        if h is None or h.pipe is None:
            return True
        if h.pipe.poll(timeout):
            try:
                msg = recv_msg(h.pipe)
            except EOFError:
                # Master pipe closed: don't stop the supervisor. Close the
                # pipe, clear the handle, and let the tick loop continue so
                # _health_check observes the dead master and restarts it.
                try:
                    h.pipe.close()
                except Exception:
                    pass
                h.pipe = None
                return True
            h.last_msg_ts = self._time_fn()
            h.fail_count = 0
            if isinstance(msg, SnapshotMsg):
                self._last_snapshot_ts = self._time_fn()
                self.heartbeat_warning = False
                snap = Snapshot(timestamp=msg.timestamp, heartbeat=msg.heartbeat,
                                positions=msg.positions)
                cmds = self._engine.ingest_snapshot(snap, now=msg.timestamp)
                for slave_id, clist in cmds.items():
                    for cmd in clist:
                        self._send(slave_id, cmd)
            elif isinstance(msg, ErrorMsg):
                self.errors.append(f"master: {msg.message}")
            return True
        if self._time_fn() - self._last_snapshot_ts > self._heartbeat_seconds * 2:
            if not self.heartbeat_warning:
                self.heartbeat_warning = True
                self.errors.append("no heartbeat from master")
        return True

    def _send(self, slave_id, msg) -> None:
        h = self._handles.get(slave_id)
        if h is None or h.pipe is None:
            return
        try:
            send_msg(h.pipe, msg)
        except (EOFError, OSError):
            self.errors.append(f"lost slave {slave_id}")

    def _health_check(self) -> None:
        for name, h in list(self._handles.items()):
            if not h.proc.is_alive():
                self._restart(name)
                continue
            if self._time_fn() - h.last_msg_ts > self._stale_seconds:
                h.fail_count += 1
                if h.fail_count >= self._consecutive_failures:
                    self._restart(name)
            else:
                h.fail_count = 0

    def _restart(self, name) -> None:
        h = self._handles.get(name)
        if h is None:
            return
        self.errors.append(f"restarting {name}")
        if h.proc.is_alive():
            h.proc.terminate()
            h.proc.join(timeout=2.0)
        if h.pipe is not None:
            try:
                h.pipe.close()
            except Exception:
                pass
        if self._kill_terminal is not None:
            try:
                self._kill_terminal(h.config.get("terminal_path", ""))
            except Exception:
                pass
        if h.role == "slave":
            self._engine.reset_slave(name)  # recovery re-seeds on reconnect
        self._handles[name] = self._spawn(name, h.role, h.config, h.password,
                                          h.adapter_kind, h.fake_state)
        if self.on_restart:
            self.on_restart(name, h.role)

    def shutdown(self) -> None:
        self._stop.set()
        for h in list(self._handles.values()):
            if h.proc.is_alive():
                h.proc.terminate()
                h.proc.join(timeout=2.0)
            if h.pipe is not None:
                try:
                    h.pipe.close()
                except Exception:
                    pass
        self._handles.clear()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            if not self.tick():
                break