"""The claim/execute/report loop.

Shape of the thing:

    heartbeat thread  ──▶ POST /heartbeat every 30s, forever
    main loop         ──▶ POST /jobs/claim ──▶ dispatch ──▶ POST /complete or /fail

The heartbeat runs on its own thread specifically so it continues *during* a long job.
The API extends every lease held by this node on each heartbeat, so a handler that runs
for ten minutes keeps its lease; if this process dies, heartbeats stop, the lease
expires, and the VPS reaper returns the job to the queue. That is the whole
crash-safety story and it needs no cleanup path on this side.

Nothing here exits because the VPS is unreachable. The desktop is expected to be
offline for hours (ARCHITECTURE.md §3): the runner backs off, keeps trying, and picks
up where it left off. The only fatal condition is a rejected token, which retrying
cannot fix.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from types import FrameType
from typing import Any, ClassVar

from agent import __version__
from agent.client import (
    ApiUnavailableError,
    AuthRejectedError,
    Job,
    PayloadRejectedError,
    WorkerClient,
)
from agent.config import RunnerConfig
from agent.handlers import NonRetryableError, dispatch, registered_types

logger = logging.getLogger(__name__)

#: Backoff bounds while the API is unreachable. Starts quick so a brief blip costs
#: nothing, caps low enough that a runner left running overnight reconnects promptly
#: once the VPS returns.
_BACKOFF_MIN = 5
_BACKOFF_MAX = 120


def _gpu_info() -> tuple[str | None, int | None]:
    """Best-effort GPU name and free VRAM, for the admin's worker panel.

    Purely informational -- a runner without nvidia-smi is still a valid runner, so
    every failure mode here returns (None, None) rather than raising.
    """
    try:
        # S607: resolved from PATH deliberately. nvidia-smi lives in a different
        # place on Windows, Linux and WSL, and this is a best-effort cosmetic probe
        # whose only failure mode is reporting no GPU.
        out = subprocess.run(
            [  # noqa: S607 - resolved from PATH deliberately, see comment above
                "nvidia-smi",
                "--query-gpu=name,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None

    if out.returncode != 0 or not out.stdout.strip():
        return None, None

    name, _, free = out.stdout.strip().splitlines()[0].partition(",")
    try:
        return name.strip()[:128], int(free.strip())
    except ValueError:
        return name.strip()[:128] or None, None


class Runner:
    def __init__(self, config: RunnerConfig, client: WorkerClient) -> None:
        self.config = config
        self.client = client
        self.stop_event = threading.Event()
        # Set when a thread stops the runner for a reason that must survive to the exit
        # code. Without it, the heartbeat thread rejecting the token would set
        # stop_event and the main loop would exit 0 -- reporting a fatal credential
        # failure as a clean shutdown, which a supervisor restarts forever in silence.
        self._fatal_exit: int | None = None
        self._active_jobs = 0
        self._gpu_name, self._gpu_vram = _gpu_info()

    # ------------------------------------------------------------------ signals
    def install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: FrameType | None) -> None:
            if self.stop_event.is_set():
                # Second signal: the operator is not waiting for a 60-second handler to
                # finish. Restore the default handler AND raise now -- merely restoring
                # it would mean this press did nothing and a THIRD was needed, which is
                # indistinguishable from a hung process.
                signal.signal(signum, signal.SIG_DFL)
                logger.warning("second signal; exiting immediately, lease will expire")
                raise KeyboardInterrupt
            logger.info("shutdown requested; finishing current job then exiting")
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, handle)

    # ------------------------------------------------------------------ heartbeat
    def _heartbeat_once(self) -> None:
        try:
            self.client.heartbeat(
                current_job_count=self._active_jobs,
                agent_version=__version__,
                capabilities={
                    "gpu": self._gpu_name is not None,
                    "handlers": list(self.config.handlers),
                },
                gpu_name=self._gpu_name,
                gpu_vram_free_mb=self._gpu_vram,
            )
        except ApiUnavailableError as exc:
            # Expected whenever the desktop's connection drops. The admin will show
            # OFFLINE after the grace window and recover on its own.
            logger.warning("heartbeat failed (will retry): %s", exc)
        except AuthRejectedError as exc:
            logger.error("%s", exc)
            self._fatal_exit = 2
            self.stop_event.set()

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            self._heartbeat_once()
            self.stop_event.wait(self.config.heartbeat_seconds)

    # ------------------------------------------------------------------ one job
    def _run_job(self, job: Job) -> None:
        logger.info(
            "claimed job", extra={"job": job.id, "type": job.job_type, "attempt": job.attempts}
        )
        self._active_jobs += 1
        try:
            result = dispatch(job.job_type, job.payload)
        except NonRetryableError as exc:
            logger.error("job %s failed permanently: %s", job.id, exc)
            self._report_fail(job, str(exc), retryable=False)
        except Exception as exc:
            logger.exception("job %s raised", job.id)
            self._report_fail(job, f"{type(exc).__name__}: {exc}", retryable=True)
        else:
            deliverable = self._deliver_side_effects(job, result)
            if deliverable is not None:
                self._report_complete(job, deliverable)
        finally:
            self._active_jobs -= 1

    #: Handler result keys that must be POSTED and then stripped, with the client call
    #: that delivers each. Two entries rather than a generic mechanism: the list is
    #: short, and being able to read what leaves this process is worth more than being
    #: able to add to it without editing.
    _DELIVERABLE: ClassVar[dict[str, str]] = {
        "embeddings": "store_embeddings",
        "articleEntities": "store_entities",
    }

    def _deliver_side_effects(self, job: Job, result: dict[str, Any]) -> dict[str, Any] | None:
        """Post anything the handler produced for its own endpoint, and strip it.

        Returns the result to complete with, or None when the job must NOT be completed.

        Vectors travel through their own endpoint rather than through `complete`,
        because `jobs.result` is kept forever and would otherwise hold a second copy of
        every embedding. Stripping them here is what keeps that true -- a handler
        returning them is the only way they could leak into the job row.

        Deliver-then-complete, never the reverse: if delivery succeeds and this process
        dies before completing, the lease expires and the batch is re-embedded to
        identical values. Completing first could mark a job done whose vectors were
        never stored, and nothing would ever revisit those articles.
        """
        key = next((k for k in self._DELIVERABLE if result.get(k)), None)
        if key is None:
            return result
        deliver = getattr(self.client, self._DELIVERABLE[key])

        try:
            outcome = deliver(str(result.get("model") or ""), result[key])
        except PayloadRejectedError as exc:
            # A dimension or model mismatch. Retrying recomputes the same rejected
            # vectors, so this fails permanently and loudly -- ADR-0005's one vector
            # space is exactly the kind of invariant that must not degrade quietly.
            logger.error("%s refused for job %s: %s", key, job.id, exc)
            self._report_fail(job, f"{key} refused: {exc}", retryable=False)
            return None
        except AuthRejectedError as exc:
            # Same fatal condition as everywhere else: retrying a dead credential looks
            # identical to being offline while never recovering.
            logger.error("%s", exc)
            self._fatal_exit = 2
            self.stop_event.set()
            return None
        except ApiUnavailableError as exc:
            # The work is done but undeliverable. Leave the job leased: it expires, the
            # reaper requeues it, and the batch is simply embedded again.
            logger.warning("could not store embeddings for %s (lease will expire): %s", job.id, exc)
            return None

        summary = {name: value for name, value in result.items() if name != key}
        summary["stored"] = outcome.get("stored", 0)
        summary["unknown"] = outcome.get("unknown", [])
        return summary

    def _report_complete(self, job: Job, result: dict[str, Any]) -> None:
        try:
            outcome = self.client.complete(job.id, result)
        except ApiUnavailableError as exc:
            # The work is done but unreportable. Do NOT retry the handler: the lease
            # will expire, the reaper will requeue, and idempotency_key is what makes
            # the second run safe. Losing the result is the correct trade.
            logger.warning("could not report completion of %s (lease will expire): %s", job.id, exc)
            return
        if outcome == "lost_lease":
            logger.warning("lease on %s expired before completion; result discarded", job.id)
        else:
            logger.info("completed job", extra={"job": job.id, "outcome": outcome})

    def _report_fail(self, job: Job, error: str, retryable: bool) -> None:
        try:
            outcome = self.client.fail(job.id, error, retryable=retryable)
        except ApiUnavailableError as exc:
            logger.warning("could not report failure of %s: %s", job.id, exc)
            return
        logger.info("reported failure", extra={"job": job.id, "outcome": outcome})

    # ------------------------------------------------------------------ startup
    def _release_orphaned_leases(self) -> None:
        """Give back leases held by a previous process of this same worker.

        Worker identity is the token, not the process. So after a crash or a kill, the
        restarted runner IS the node that still holds the dead process's leases -- and
        its heartbeats extend them, because the API refreshes every lease belonging to
        the node. The job would then never expire (heartbeats keep it alive) and never
        run (this process does not know it exists). Stuck forever, silently.

        Observed in the wild: a job killed mid-flight showed 894s of a 900s lease
        remaining several minutes after the runner that claimed it had died.

        Reporting them as retryable failures puts them straight back on the queue with
        the standard backoff, which is what the reaper would have done had the lease
        been allowed to lapse.
        """
        try:
            status = self.client.status()
        except (ApiUnavailableError, AuthRejectedError) as exc:
            # Not fatal: the loop retries, and the leases stay stuck only until the next
            # successful start. Better than refusing to run at all.
            logger.warning("could not check for orphaned leases: %s", exc)
            return

        orphans = status.get("leasedJobs") or []
        if not orphans:
            return

        logger.warning(
            "releasing %d lease(s) held by a previous run of this worker: %s",
            len(orphans),
            ", ".join(orphans),
        )
        for job_id in orphans:
            try:
                self.client.fail(
                    job_id,
                    "runner restarted while holding this lease; returning it to the queue",
                    retryable=True,
                )
            except (ApiUnavailableError, AuthRejectedError) as exc:
                logger.warning("could not release lease %s: %s", job_id, exc)

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        logger.info(
            "runner starting: %s -> %s, handlers=%s",
            self.config.worker_name,
            self.config.api_url,
            ",".join(self.config.handlers),
        )

        # BEFORE the heartbeat starts: the first beat would otherwise extend exactly the
        # stale leases we are about to release.
        self._release_orphaned_leases()

        heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        heartbeat.start()

        backoff = _BACKOFF_MIN
        while not self.stop_event.is_set():
            try:
                jobs = self.client.claim(
                    handlers=list(self.config.handlers),
                    max_jobs=self.config.max_jobs,
                    lease_seconds=self.config.lease_seconds,
                )
            except AuthRejectedError as exc:
                logger.error("%s", exc)
                self._fatal_exit = 2
                return 2
            except ApiUnavailableError as exc:
                logger.warning("claim failed, backing off %ss: %s", backoff, exc)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            backoff = _BACKOFF_MIN

            if not jobs:
                self.stop_event.wait(self.config.idle_poll_seconds)
                continue

            for job in jobs:
                self._run_job(job)
                # Finish the batch even while shutting down: these leases are already
                # ours, and abandoning them means waiting out the lease before anyone
                # can pick them up.

        logger.info("runner stopped")
        return self._fatal_exit or 0


def build_runner(config: RunnerConfig) -> Runner:
    client = WorkerClient(config.api_url, config.token)
    return Runner(config, client)


def advertised_handlers() -> tuple[str, ...]:
    """What this build can dispatch. Single source of truth for the claim call."""
    return registered_types()
