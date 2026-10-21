"""Background jobs for long builds.

A remote chat call is cut off after a few minutes, but a big Feature Graph can take longer, and the
caller who times out may re-send it while the first copy is still building. With `background=True` a
build tool starts here and returns a job id at once; the model polls `job_status` (a bounded wait,
with progress) and can cancel between operations.

One job at a time: SolidWorks serialises all COM work anyway, and two interleaved builds would corrupt
each other's geometry. While a job runs, every OTHER CAD call is refused with a clear message (the job's
own thread is exempt), so a stray tool call cannot slip a feature into the middle of a build.
"""
import threading
import time
import uuid

POLL_CAP_S = 25.0            # one job_status wait stays under typical relay/host request limits
_lock = threading.Lock()
_jobs: dict = {}
_order: list = []            # job ids, oldest first
_KEEP = 20                   # finished jobs remembered


class JobCancelled(Exception):
    """Raised inside the job's thread at the next operation after a cancel request."""


class JobBusy(Exception):
    def __init__(self, job_id):
        super().__init__(job_id)
        self.job_id = job_id


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = "RUNNING"            # RUNNING | COMPLETED | FAILED | CANCELLED
        self.started = time.monotonic()
        self.ended = None
        self.ops = 0
        self.last_tool = None
        self.result = None
        self.cancel_requested = False
        self.thread_id = None
        self.done = threading.Event()

    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started


def active():
    with _lock:
        for jid in reversed(_order):
            if _jobs[jid].state == "RUNNING":
                return _jobs[jid]
    return None


def blocked_for_this_thread():
    """The running job, if the CURRENT thread is not the one running it (so its CAD calls must wait)."""
    job = active()
    if job is not None and job.thread_id != threading.get_ident():
        return job
    return None


def checkpoint(tool: str) -> None:
    """Called before every operation the job executes: counts it and honours a cancel request."""
    job = active()
    if job is None or job.thread_id != threading.get_ident():
        return
    job.ops += 1
    job.last_tool = tool
    if job.cancel_requested:
        raise JobCancelled("cancelled by request before %s" % tool)


def launch(kind: str, fn) -> str:
    """Start fn() on a worker thread. -> the model-facing JOB_STARTED line (or JOB_BUSY)."""
    with _lock:
        for jid in reversed(_order):
            if _jobs[jid].state == "RUNNING":
                return ("JOB_BUSY | job %s (%s) is still running; one build at a time. Poll it with "
                        "job_status, or cancel it first." % (jid, _jobs[jid].kind))
        job = Job(kind)
        _jobs[job.id] = job
        _order.append(job.id)
        while len(_order) > _KEEP:
            old = _order.pop(0)
            if _jobs[old].state != "RUNNING":
                _jobs.pop(old, None)
            else:
                _order.insert(0, old)
                break

    def run():
        job.thread_id = threading.get_ident()
        try:
            out = fn()
            job.result = out if isinstance(out, str) else str(out)
            job.state = "COMPLETED" if not job.result.startswith("FAILED") else "FAILED"
        except JobCancelled as ex:
            job.result = "CANCELLED | %s | partial geometry may remain (CAD ops are not transactional)" % ex
            job.state = "CANCELLED"
        except BaseException as ex:                        # noqa: BLE001 - a job must always end
            job.result = "FAILED | JOB_CRASHED | %s: %s" % (type(ex).__name__, ex)
            job.state = "FAILED"
        finally:
            job.ended = time.monotonic()
            job.done.set()

    # thread_id must be known before the first checkpoint: set inside run(); the thread starts here.
    t = threading.Thread(target=run, name="solidpilot-job-" + job.id, daemon=True)
    t.start()
    for _ in range(200):                                    # ~1 s at most: wait until run() has registered its id
        if job.thread_id is not None or job.done.is_set():
            break
        time.sleep(0.005)
    return ("JOB_STARTED | job_id=%s | %s is running in the background — poll job_status(job_id='%s'); "
            "do not start other CAD calls until it finishes." % (job.id, kind, job.id))


def status(job_id: str = "", wait_s: float = 20.0, cancel: bool = False) -> str:
    with _lock:
        if job_id:
            job = _jobs.get(job_id)
        else:
            job = _jobs[_order[-1]] if _order else None
    if job is None:
        return "FAILED | JOB_NOT_FOUND | %s" % (job_id or "no job has been started")
    if cancel and job.state == "RUNNING":
        job.cancel_requested = True
    deadline = time.monotonic() + max(0.0, min(float(wait_s), POLL_CAP_S))
    while job.state == "RUNNING" and time.monotonic() < deadline:
        job.done.wait(0.25)
    if job.state == "RUNNING":
        note = " | cancel requested: stops before its next operation" if job.cancel_requested else ""
        return ("RUNNING | job_id=%s | %s | %.0fs elapsed | ops=%d | last_tool=%s%s"
                % (job.id, job.kind, job.elapsed(), job.ops, job.last_tool, note))
    return "%s | job_id=%s | %s | %.0fs | ops=%d\n%s" % (job.state, job.id, job.kind, job.elapsed(), job.ops, job.result)
