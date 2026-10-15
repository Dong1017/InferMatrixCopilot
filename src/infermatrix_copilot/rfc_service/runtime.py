"""Worker wakeups and independent, content-free process heartbeats."""
from __future__ import annotations

import threading
import uuid

from .store import encode


class WorkerMonitor:
    def __init__(self, service, stop):
        self.service, self.stop = service, stop
        self.process_id = uuid.uuid4().hex
        self.states = {}
        self.thread = None
        self.lock = threading.Lock()

    def register(self, kind, name):
        with self.lock:
            self.states[name] = {"kind": kind, "busy": False, "error": False}
        return name

    def state(self, name, *, busy=False, error=False):
        with self.lock:
            self.states[name].update(busy=bool(busy), error=bool(error))

    def flush(self, *, stopped=False):
        with self.lock:
            values = list(self.states.items())
        with self.service.store.transaction() as con:
            for name, state in values:
                con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (
                    "worker:" + self.process_id + ":" + name,
                    encode({**state, "updated": self.service.clock(), "stopped": stopped})))
            # A bounded diagnostics view for services restarted repeatedly.
            con.execute("DELETE FROM metadata WHERE key LIKE 'worker:%' AND CAST(json_extract(value,'$.updated') AS REAL)<?", (self.service.clock() - 86400,))

    def start(self):
        self.flush()
        def run():
            while not self.stop.wait(2):
                try:
                    self.flush()
                except Exception:
                    # The next heartbeat will expose a stale worker; never log
                    # a database exception containing private service paths.
                    pass
            try:
                self.flush(stopped=True)
            except Exception:
                pass
        self.thread = threading.Thread(target=run, name="rfc-worker-monitor", daemon=True)
        self.thread.start()

    def join(self):
        if self.thread:
            self.thread.join(timeout=2)


def wait_for_work(service, stop, interval=1):
    """Events wake same-process work; <=1s polling covers other processes."""
    if not stop.is_set():
        service.store.work_available.wait(min(1, max(0.01, interval)))
