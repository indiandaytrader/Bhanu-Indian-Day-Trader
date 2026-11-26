# web/detail_jobs.py
"""Bounded, shared work for expensive dashboard reads requested by the browser."""
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from time import monotonic


class DetailJobs:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dashboard-detail")
        self.lock = RLock()
        self.jobs = {}

    def _completed(self, key, future):
        with self.lock:
            self.jobs[key] = future, monotonic() + 60

    def read(self, key, build):
        with self.lock:
            now = monotonic()
            expired = [k for k,(future,until) in self.jobs.items() if future.done() and until < now]
            for k in expired:
                del self.jobs[k]
            if key not in self.jobs:
                if len(self.jobs) >= 64:
                    return 503, {"error":"Dashboard busy. Try again shortly."}
                future = self.pool.submit(build)
                self.jobs[key] = future, float("inf")
                future.add_done_callback(lambda done: self._completed(key, done))
            future, until = self.jobs[key]
            if not future.done():
                return 202, {"status":"loading"}
            try:
                return 200, future.result()
            except Exception:
                return 503, {"error":"Detail data unavailable. Try again shortly."}
