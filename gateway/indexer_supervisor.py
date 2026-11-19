"""Keep the single thread indexer alive without doing work in HTTP handlers."""
import threading
import time


class IndexerSupervisor:
    def __init__(self, find_owner, spawn, log, interval=5, retry_seconds=10,
                 clock=time.monotonic):
        self.find_owner = find_owner
        self.spawn = spawn
        self.log = log
        self.interval = interval
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.worker = None
        self.next_attempt = 0
        self.state = {"pid": None, "state": "starting", "restarts": 0}

    def status(self):
        with self.lock:
            return dict(self.state)

    def ensure(self):
        with self.lock:
            if self.worker is not None:
                if hasattr(self.worker, "poll"):
                    alive = self.worker.poll() is None
                else:
                    alive = self.worker.is_alive()
                if alive:
                    return dict(self.state)
                self.log("indexer: worker exited; scheduling recovery")
                self.worker = None
                self.state.update(pid=None, state="recovering")
            owner = self.find_owner()
            if owner:
                self.state.update(pid=owner, state="running")
                return dict(self.state)
            self.state.update(pid=None, state="recovering")
            if self.clock() < self.next_attempt:
                return dict(self.state)
            self.next_attempt = self.clock() + self.retry_seconds
            try:
                self.worker = self.spawn()
                if self.worker is None:
                    raise RuntimeError("indexer worker was not created")
                pid = getattr(self.worker, "pid", None)
                self.state.update(pid=pid, state="running",
                                  restarts=self.state["restarts"] + 1)
                self.state.pop("error", None)
                self.log("indexer: supervised worker started pid=%s" % pid)
            except Exception as exc:
                self.state.update(state="recovering", error=type(exc).__name__)
                self.log("indexer: start failed (%s); will retry" % type(exc).__name__)
            return dict(self.state)

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            self.thread = threading.Thread(target=self._watch,
                                           name="indexer-supervisor", daemon=True)
            self.thread.start()

    def _watch(self):
        while not self.stop_event.is_set():
            try:
                self.ensure()
            except Exception as exc:
                self.log("indexer: supervisor check failed (%s)" % type(exc).__name__)
            self.stop_event.wait(self.interval)

    def stop(self):
        # The indexer is a shared single instance; stopping its supervisor does
        # not kill a worker another gateway may have adopted during a restart.
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.interval + 1)
