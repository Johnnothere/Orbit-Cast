"""
ORBITCAST — the analysis queue.

WHY THIS EXISTS
A CV analysis is three to four Claude calls in a row (extract, score, critique,
and for thin profiles a 60-90s web-search orientation). It used to run inside
the HTTP request, on one of the web server's 8 threads. Eight people pressing
"Get matched" at the same moment took every thread for a minute or two, and
the Events page, sign-in and everything else queued behind them.

Now the request only validates the input and hands the work to this queue,
which answers in milliseconds with a job id. A fixed pool of worker threads
(ANALYSIS_WORKERS) runs the analyses; the browser polls for the result. Web
threads stay free for browsing no matter how many analyses are waiting, and
the number of simultaneous Claude pipelines is bounded, so a crowd arriving at
once becomes a short queue with a visible position instead of a pile of
timeouts.

WHAT IT DELIBERATELY IS NOT
In-memory, single-process. That matches how OrbitCast runs (one Gunicorn
worker, see Procfile) and needs no Redis or new table. The cost: a restart or
deploy drops queued and running jobs. The browser is told the job is gone and
offers to run it again - nothing is half-saved, because persistence happens at
the very end of a job in one place.
"""

import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("orbitcast.jobs")

ANALYSIS_WORKERS = max(1, int(os.environ.get("ANALYSIS_WORKERS", "24")))
ANALYSIS_QUEUE_MAX = max(1, int(os.environ.get("ANALYSIS_QUEUE_MAX", "300")))
RESULT_TTL_S = 15 * 60          # a finished result is kept this long for polling
STALE_S = 20 * 60               # a job unfinished after this is reported failed


class QueueFull(Exception):
    pass


class _Job:
    __slots__ = ("id", "owner", "kind", "status", "created", "started", "finished",
                 "http_status", "payload")

    def __init__(self, owner, kind):
        self.id = secrets.token_urlsafe(16)
        self.owner = owner
        self.kind = kind
        self.status = "queued"          # queued -> running -> done | error
        self.created = time.time()
        self.started = None
        self.finished = None
        self.http_status = None
        self.payload = None


class JobQueue:
    def __init__(self, workers=ANALYSIS_WORKERS, max_queue=ANALYSIS_QUEUE_MAX):
        self.workers = workers
        self.max_queue = max_queue
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analysis")
        self._lock = threading.Lock()
        self._jobs: "OrderedDict[str, _Job]" = OrderedDict()
        self._waiting: "OrderedDict[str, None]" = OrderedDict()   # queued ids, in order
        self._avg_s = 45.0          # moving average of run time, for the ETA

    # ── public ──────────────────────────────────────────────────────────────
    def active_for(self, owner):
        """The caller's queued or running job, if any. One at a time per
        person: a double click, a second tab or an impatient retry gets the
        job already in flight instead of a second place in the queue."""
        with self._lock:
            self._purge()
            for job in self._jobs.values():
                if job.owner == owner and job.status in ("queued", "running"):
                    return self._view(job)
        return None

    def submit(self, owner, kind, fn, *args, **kwargs):
        """fn(*args, **kwargs) -> (http_status, payload_dict). Raises QueueFull."""
        with self._lock:
            self._purge()
            if len(self._waiting) >= self.max_queue:
                raise QueueFull()
            job = _Job(owner, kind)
            self._jobs[job.id] = job
            self._waiting[job.id] = None
            view = self._view(job)
        self._pool.submit(self._run, job, fn, args, kwargs)
        return view

    def get(self, job_id, owner):
        """The job's current state, or None if it does not exist, has expired,
        or belongs to someone else (indistinguishable on purpose)."""
        with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
            if job is None or job.owner != owner:
                return None
            return self._view(job)

    def stats(self):
        with self._lock:
            self._purge()
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            return {"queued": len(self._waiting), "running": running,
                    "workers": self.workers, "queue_max": self.max_queue,
                    "avg_run_s": round(self._avg_s, 1)}

    # ── internals ───────────────────────────────────────────────────────────
    def _run(self, job, fn, args, kwargs):
        with self._lock:
            self._waiting.pop(job.id, None)
            if job.status != "queued":              # expired while waiting
                return
            job.status = "running"
            job.started = time.time()
        try:
            status, payload = fn(*args, **kwargs)
        except Exception as exc:                    # never leave a job hanging
            log.exception(f"analysis job {job.kind} crashed: {exc}")
            status, payload = 500, {"error": "The analysis failed on our side. Please try again."}
        with self._lock:
            job.finished = time.time()
            job.http_status = int(status)
            job.payload = payload
            job.status = "done" if 200 <= job.http_status < 300 else "error"
            took = job.finished - job.started
            self._avg_s = 0.8 * self._avg_s + 0.2 * took

    def _view(self, job):
        out = {"job": job.id, "status": job.status, "kind": job.kind}
        now = time.time()
        if job.status == "queued":
            ahead = 0
            for jid in self._waiting:
                if jid == job.id:
                    break
                ahead += 1
            out["position"] = ahead + 1
            # Everyone ahead of you, plus the jobs already running, spread
            # over the worker pool, at the recent average run time.
            rounds = (ahead // self.workers) + 1
            out["eta_s"] = int(rounds * self._avg_s)
        elif job.status == "running":
            out["elapsed_s"] = int(now - job.started)
        else:
            out["http_status"] = job.http_status
            if job.status == "done":
                out["result"] = job.payload
            else:
                out["error"] = (job.payload or {}).get("error") or "The analysis failed. Please try again."
        return out

    def _purge(self):
        now = time.time()
        dead = []
        for jid, job in self._jobs.items():
            if job.status in ("done", "error"):
                if now - job.finished > RESULT_TTL_S:
                    dead.append(jid)
            elif now - job.created > STALE_S:
                # Should not happen (Claude calls time out well before this),
                # but a job must never poll as "running" forever.
                job.status = "error"
                job.finished = now
                job.http_status = 504
                job.payload = {"error": "That analysis took too long and was stopped. Please try again."}
                self._waiting.pop(jid, None)
        for jid in dead:
            self._jobs.pop(jid, None)
