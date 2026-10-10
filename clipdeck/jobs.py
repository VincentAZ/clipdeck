"""A small job queue: N worker threads pulling Plans, reporting through a callback.
UI-agnostic; the GTK layer marshals callbacks onto the main loop."""

from __future__ import annotations

import itertools
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from .ops import Plan
from .runner import Result, execute

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
_ids = itertools.count(1)
_SKIP = object()


@dataclass(eq=False)
class Job:
    plan: Plan
    id: int = field(default_factory=lambda: next(_ids))
    state: str = QUEUED
    progress: float = 0.0
    status: str = "Waiting"
    result: Result | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    after: "Job | None" = None      # do not start before this job has finished

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELLED)


class JobQueue:
    def __init__(self, on_change: Callable[[Job], None], workers: int = 1,
                 thorough: Callable[[], bool] = lambda: True):
        self.on_change = on_change
        self.thorough = thorough
        self.jobs: list[Job] = []
        self._cv = threading.Condition()
        self._workers: list[threading.Thread] = []
        self._max = max(1, workers)
        self._running = 0
        self._closing = False
        self._reserved: set[str] = set()   # outputs claimed by queued/running jobs
        self._skipped: list[Job] = []      # failed because a job they waited for failed

    # ---------------------------------------------------------------- public
    def submit(self, plan: Plan, after: Job | None = None) -> Job:
        job = Job(plan, after=after)
        with self._cv:
            self.jobs.append(job)
            self._reserved.add(plan.output)
            self._ensure_workers()
            self._cv.notify_all()
        self._notify(job)
        return job

    def reserved_outputs(self) -> set[str]:
        with self._cv:
            return set(self._reserved)

    def cancel(self, job: Job):
        with self._cv:
            if job.state == QUEUED:
                job.state = CANCELLED
                job.status = "Cancelled"
                self._reserved.discard(job.plan.output)
                notify = True
            else:
                notify = False
            job.cancel.set()
            self._cv.notify_all()   # jobs waiting on this one may start now
        if notify:
            self._notify(job)

    def cancel_all(self):
        for j in list(self.jobs):
            if not j.finished:
                self.cancel(j)

    def clear_finished(self):
        with self._cv:
            self.jobs = [j for j in self.jobs if not j.finished]

    def set_workers(self, n: int):
        with self._cv:
            self._max = max(1, n)
            self._ensure_workers()
            self._cv.notify_all()

    def active_count(self) -> int:
        with self._cv:
            return sum(1 for j in self.jobs if not j.finished)

    def shutdown(self, wait: float = 8.0):
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        self.cancel_all()
        deadline = time.monotonic() + wait          # one shared budget, not per worker
        for t in self._workers:
            t.join(max(0.0, deadline - time.monotonic()))
        # a worker that did not stop in time must not leave a hidden partial file behind
        for j in self.jobs:
            if not j.finished:
                for f in [j.plan.temp, *j.plan.cleanup]:
                    try:
                        os.unlink(f)
                    except OSError:
                        pass

    # ---------------------------------------------------------------- workers
    def _ensure_workers(self):
        alive = [t for t in self._workers if t.is_alive()]
        self._workers = alive
        while len(self._workers) < self._max:
            t = threading.Thread(target=self._loop, daemon=True, name="clipdeck-worker")
            self._workers.append(t)
            t.start()

    @staticmethod
    def _sources(job: Job) -> set[str]:
        return {os.path.realpath(i.path) for i in job.plan.inputs}

    def _ready(self, job: Job) -> bool:
        if job.after is not None and not job.after.finished:
            return False
        srcs = self._sources(job)
        for other in self.jobs:
            if other is job or other.finished or not (srcs & self._sources(other)):
                continue
            if job.plan.replace_original:
                # the original goes to the Trash when this finishes: jobs queued earlier
                # that read the same file go first, and nothing else may be reading it
                if other.state == RUNNING or self.jobs.index(other) < self.jobs.index(job):
                    return False
            elif other.plan.replace_original and other.state == RUNNING:
                # a job queued later must not read a file that is being replaced under it;
                # it starts afterwards and then fails cleanly ("source changed")
                return False
        return True

    def _blocked_by_failure(self, job: Job) -> str:
        a = job.after
        if a is None or not a.finished or a.state == DONE:
            return ""
        return (f"not started: {os.path.basename(a.plan.output)} "
                f"{'was cancelled' if a.state == CANCELLED else 'failed'}, so the original "
                "was left untouched" if job.plan.replace_original else
                f"not started: {os.path.basename(a.plan.output)} did not finish")

    def _notify(self, job: Job):
        try:
            self.on_change(job)
        except Exception:  # noqa: BLE001 - a UI callback must never kill a worker
            traceback.print_exc()

    def _next(self) -> Job | None:
        with self._cv:
            while True:
                if self._closing:
                    return None
                if len(self._workers) > self._max:
                    # pool shrank: this idle worker leaves
                    self._workers = [t for t in self._workers
                                     if t is not threading.current_thread()]
                    return None
                if self._running < self._max:
                    for j in self.jobs:
                        if j.state == QUEUED and (why := self._blocked_by_failure(j)):
                            j.result = Result(ok=False, failures=[why])   # before the state
                            j.state, j.status = FAILED, "Failed"
                            self._reserved.discard(j.plan.output)
                            self._skipped.append(j)
                            self._cv.notify_all()
                    if self._skipped:
                        return _SKIP           # report them outside the lock first
                    for j in self.jobs:
                        if j.state == QUEUED and self._ready(j):
                            j.state = RUNNING
                            j.status = "Starting"
                            self._running += 1
                            return j
                self._cv.wait()

    def _loop(self):
        while True:
            job = self._next()
            if job is _SKIP:
                with self._cv:
                    skipped, self._skipped = self._skipped, []
                for s in skipped:
                    self._notify(s)
                continue
            if job is None:
                return
            self._notify(job)

            def progress(frac: float, text: str, j=job):
                j.progress = frac
                j.status = text
                self._notify(j)
            try:
                res = execute(job.plan, job.cancel, progress, thorough=self.thorough())
            except Exception as e:  # execute already guards; belt and braces
                res = Result(ok=False, failures=[str(e)])
            with self._cv:
                job.result = res
                if res.ok:
                    job.state, job.status, job.progress = DONE, "Done", 1.0
                elif job.cancel.is_set():
                    job.state, job.status = CANCELLED, "Cancelled"
                else:
                    job.state, job.status = FAILED, "Failed"
                self._running -= 1
                self._reserved.discard(job.plan.output)
                self._cv.notify_all()
            self._notify(job)
