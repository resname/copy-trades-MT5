from __future__ import annotations

import multiprocessing
import threading
import time
from dataclasses import dataclass

from manager.engine.copy_loop import CopyEngine
from manager.engine.models import Snapshot
from manager.ipc.messages import (
    AckMsg, StatusMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, ErrorMsg,
    ReconfigureMsg, StartMsg,
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
    adapter_kind: str
    fake_state: dict | None
    got_symbol_info: bool = False
    got_status: bool = False
    restart_count: int = 0
    next_restart_at: float = 0.0
    last_msg_ts: float = 0.0
    fail_count: int = 0
    fatal: bool = False  # set on a fatal ErrorMsg; _health_check won't restart
    first_msg_seen: bool = False  # False until first message -> grace window


class Supervisor:
    """Owns worker subprocesses + pipes; routes IPC to/from the CopyEngine;
    watches health and restarts on death. tick() = one slave drain + one timed
    master poll + a health pass. _run() loops it in a daemon thread."""

    BASE_BACKOFF = 1.0   # seconds; first respawn delay after a death
    MAX_BACKOFF = 30.0   # seconds; cap on exponential backoff
    STARTUP_GRACE_SECONDS = 90.0  # first-message grace after (re)spawn

    def __init__(self, engine: CopyEngine, heartbeat_seconds: int = 5,
                 stale_seconds: float = 30.0, consecutive_failures: int = 3,
                 poll_timeout: float = 0.2, time_fn=time.time,
                 kill_terminal=None,
                 startup_grace_seconds: float = STARTUP_GRACE_SECONDS):
        self._engine = engine
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_seconds = stale_seconds
        self._consecutive_failures = consecutive_failures
        self._poll_timeout = poll_timeout
        self._time_fn = time_fn
        self._kill_terminal = kill_terminal  # callable(path) or None (Plan 3)
        self._startup_grace_seconds = startup_grace_seconds
        self._handles: dict[str, WorkerHandle] = {}
        self._last_snapshot_ts = time_fn()
        self.heartbeat_warning = False
        self._master_first_snapshot_seen = False
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = None
        self.on_restart = None  # callback(name, role) for GUI status (Plan 4)
        self.on_error = None    # callback(name, message) for GUI status/log

    def spawn_master(self, config, adapter_kind="real", fake_state=None):
        self._handles["master"] = self._spawn("master", "master", config,
                                               adapter_kind, fake_state)
        self._reset_master_heartbeat()

    def spawn_slave(self, slave_id, config, adapter_kind="real", fake_state=None):
        self._handles[slave_id] = self._spawn(slave_id, "slave", config,
                                              adapter_kind, fake_state)

    def _reset_master_heartbeat(self) -> None:
        """Start a fresh heartbeat grace window: the master gets up to
        STARTUP_GRACE_SECONDS to produce its first SnapshotMsg before 'no
        heartbeat' fires. Called on initial spawn and on every master restart."""
        self._last_snapshot_ts = self._time_fn()
        self._master_first_snapshot_seen = False
        self.heartbeat_warning = False

    def slave_ready(self, slave_id) -> bool:
        """A slave is ready once it has reported BOTH SymbolInfoMsg and its
        first StatusMsg (balance). Until then, spawning the master risks the
        startup-race permanent-skip (Plan 2 deferred MUST #1)."""
        h = self._handles.get(slave_id)
        return h is not None and h.got_symbol_info and h.got_status

    def wait_for_slaves_ready(self, timeout: float = 10.0,
                              slave_ids: list[str] | None = None) -> bool:
        """Tick until every spawned slave (or the given ids) is ready or the
        timeout elapses. Call BEFORE spawn_master. Returns whether all are
        ready. Safe when no master is spawned yet (_read_master is a no-op
        when the master handle is absent)."""
        ids = slave_ids if slave_ids is not None \
            else [n for n, h in self._handles.items() if h.role == "slave"]
        if not ids:
            return True
        deadline = self._time_fn() + timeout
        while self._time_fn() < deadline:
            self.tick(timeout=0.02)
            if all(self.slave_ready(i) for i in ids):
                return True
        return all(self.slave_ready(i) for i in ids)

    def _spawn(self, name, role, config, adapter_kind, fake_state):
        parent_pipe, child_pipe = multiprocessing.Pipe(duplex=True)
        proc = multiprocessing.Process(target=worker_main,
            args=(child_pipe, role, adapter_kind, fake_state), daemon=True)
        proc.start()
        child_pipe.close()  # parent owns only the parent end
        send_msg(parent_pipe, StartMsg(config=config))
        return WorkerHandle(name=name, role=role, proc=proc, pipe=parent_pipe,
                            config=config, adapter_kind=adapter_kind,
                            fake_state=fake_state, last_msg_ts=self._time_fn())

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
            while h.pipe is not None:
                # On Windows, poll(0) on a peer-closed pipe with no buffered
                # data raises BrokenPipeError (not EOFError, not False); a
                # dead worker hits this, so treat it as "pipe closed".
                try:
                    if not h.pipe.poll(0):
                        break
                except (BrokenPipeError, OSError):
                    self.errors.append(f"slave {name} pipe closed")
                    h.pipe = None
                    break
                try:
                    msg = recv_msg(h.pipe)
                except EOFError:
                    self.errors.append(f"slave {name} pipe closed")
                    h.pipe = None
                    break
                self._dispatch_slave(name, msg)
                h.last_msg_ts = self._time_fn()
                h.fail_count = 0
                h.first_msg_seen = True

    def _dispatch_slave(self, slave_id, msg) -> None:
        h = self._handles.get(slave_id)
        if h is not None:
            h.got_symbol_info = h.got_symbol_info or isinstance(msg, SymbolInfoMsg)
            h.got_status = h.got_status or isinstance(msg, StatusMsg)
            h.restart_count = 0
            h.next_restart_at = 0.0
        if isinstance(msg, AckMsg):
            if not msg.ok:
                # Surface a failed order so the user sees WHY a trade didn't
                # copy (e.g. MT5 rejected the comment/filling mode) instead of
                # apply_ack silently leaving the slave_ticket=0 marker. The
                # marker stays, so this fires once per failed trade (no re-NEW).
                self._surface_error(
                    slave_id,
                    f"{slave_id}: {msg.action} #{msg.master_ticket} failed "
                    f"(retcode {msg.retcode}): {msg.error}")
            for cmd in self._engine.apply_ack(slave_id, msg):
                self._send(slave_id, cmd)
        elif isinstance(msg, StatusMsg):
            self._engine.apply_status(slave_id, msg)
        elif isinstance(msg, RecoveryMsg):
            self._engine.apply_recovery(slave_id, msg.records)
        elif isinstance(msg, SymbolInfoMsg):
            self._engine.apply_symbol_info(slave_id, msg.infos)
        elif isinstance(msg, ErrorMsg):
            if msg.fatal and h is not None:
                h.fatal = True
            self._surface_error(slave_id, f"{slave_id}: {msg.message}")

    def _read_master(self, timeout) -> bool:
        h = self._handles.get("master")
        if h is None or h.pipe is None:
            return True
        try:
            readable = h.pipe.poll(timeout)
        except (BrokenPipeError, OSError):
            # peer-closed pipe with no buffered data (dead master): same as the
            # EOF path below — close the pipe and let _health_check decide.
            try:
                h.pipe.close()
            except Exception:
                pass
            h.pipe = None
            return True
        if readable:
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
            h.restart_count = 0
            h.next_restart_at = 0.0
            h.first_msg_seen = True
            if isinstance(msg, SnapshotMsg):
                self._last_snapshot_ts = self._time_fn()
                self._master_first_snapshot_seen = True
                self.heartbeat_warning = False
                snap = Snapshot(timestamp=msg.timestamp, heartbeat=msg.heartbeat,
                                positions=msg.positions)
                cmds = self._engine.ingest_snapshot(snap, now=msg.timestamp)
                for slave_id, clist in cmds.items():
                    for cmd in clist:
                        self._send(slave_id, cmd)
            elif isinstance(msg, StatusMsg):
                pass
            elif isinstance(msg, ErrorMsg):
                if msg.fatal:
                    h.fatal = True
                self._surface_error("master", f"master: {msg.message}")
            return True
        hb = (self._startup_grace_seconds if not self._master_first_snapshot_seen
              else self._heartbeat_seconds * 2)
        if self._time_fn() - self._last_snapshot_ts > hb:
            if not self.heartbeat_warning:
                self.heartbeat_warning = True
                self._surface_error("master", "no heartbeat from master")
        return True

    def _surface_error(self, name: str, message: str) -> None:
        """Record a worker/runtime error and forward it to the GUI (on_error)
        so the user sees WHY something failed instead of a silent symptom
        (e.g. a terminal that blinks open/closed because initialize failed)."""
        self.errors.append(message)
        if self.on_error is not None:
            self.on_error(name, message)

    def _send(self, slave_id, msg) -> None:
        h = self._handles.get(slave_id)
        if h is None or h.pipe is None:
            return
        try:
            send_msg(h.pipe, msg)
        except (EOFError, OSError):
            self.errors.append(f"lost slave {slave_id}")

    def reconfigure_slave(self, slave_id: str, symbol_map_csv: str,
                          normalize_sltp: bool) -> None:
        """Live-update a running slave's symbol map + normalize flag. Always
        updates h.config so a subsequent _restart spawns with the new params
        (a dead non-fatal worker is restarted by _health_check and picks up the
        edit). Sends the ReconfigureMsg only when the pipe is open and the
        worker is not fatal. No-op when the handle is missing."""
        h = self._handles.get(slave_id)
        if h is None:
            return
        h.config["symbol_map_csv"] = symbol_map_csv
        h.config["normalize_sltp"] = normalize_sltp
        if h.pipe is None or h.fatal:
            return  # worker gone/fatal: can't send, but h.config is updated
        self._send(slave_id, ReconfigureMsg(
            source_id=slave_id, symbol_map_csv=symbol_map_csv,
            normalize_sltp=normalize_sltp))

    def _health_check(self) -> None:
        for name, h in list(self._handles.items()):
            if h.fatal:
                # A worker that reported a FATAL error (e.g. mt5.initialize
                # failed because the terminal isn't logged in) must not be
                # restarted: _restart calls kill_terminal (closes the
                # terminal) then re-spawns (mt5.initialize re-opens it), which
                # fails the same way -> the terminal would blink open/closed
                # forever. Leave it dead; the user sees the surfaced error.
                continue
            if not h.proc.is_alive():
                self._restart(name)
                continue
            stale = (self._startup_grace_seconds if not h.first_msg_seen
                     else self._stale_seconds)
            if self._time_fn() - h.last_msg_ts > stale:
                h.fail_count += 1
                if h.fail_count >= self._consecutive_failures:
                    self._restart(name)
            else:
                h.fail_count = 0

    def _restart(self, name) -> None:
        h = self._handles.get(name)
        if h is None:
            return
        now = self._time_fn()
        # Backoff: a *dead* worker is not respawned until next_restart_at.
        # A *stale-but-alive* worker (consecutive stale failures) is restarted
        # immediately — the process is hung, not dead, so there is no spawn
        # storm to dampen.
        if not h.proc.is_alive():
            if now < h.next_restart_at:
                return  # still in the backoff window; retry next tick
            self.errors.append(f"restarting {name}")
        else:
            self.errors.append(f"restarting {name} (stale)")
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
        # Schedule the backoff for the NEXT death from the CURRENT count,
        # then carry that state onto the freshly-spawned handle (the new
        # handle defaults to restart_count=0/next_restart_at=0.0, so without
        # carrying it over a respawn would reset the backoff and a death storm
        # would never throttle).
        delay = min(self.BASE_BACKOFF * (2 ** h.restart_count), self.MAX_BACKOFF)
        new_count = h.restart_count + 1
        new_next = now + delay
        new_h = self._spawn(name, h.role, h.config, h.adapter_kind, h.fake_state)
        new_h.restart_count = new_count
        new_h.next_restart_at = new_next
        self._handles[name] = new_h
        if h.role == "master":
            self._reset_master_heartbeat()
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