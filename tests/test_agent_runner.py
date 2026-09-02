"""The desktop runner's contract with the VPS.

These pin the behaviours ADR-0001 promises and that are easy to get wrong in a loop
nobody watches:

  * an unreachable VPS is a normal condition, not a crash — the desktop is expected to
    be offline for hours, and a runner that exits on a connection error would need a
    human to restart it every time the network hiccups;
  * a handler that raises must not take the process down, and must report the failure
    so the job is retried rather than sitting leased until the reaper collects it;
  * a lease lost mid-job (409) must discard the result rather than double-applying it;
  * only registered handlers are advertised, so the API cannot lease work this build
    cannot dispatch;
  * a rejected token is fatal, because retrying it forever would look identical to
    being offline while never recovering.

Everything runs against an httpx.MockTransport speaking the real protocol from
services/api/app/routers/worker.py, so no services are needed.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "agent-runner"))

from agent.client import (  # noqa: E402
    ApiUnavailableError,
    AuthRejectedError,
    Job,
    WorkerClient,
)
from agent.config import ConfigError, RunnerConfig, load_config  # noqa: E402
from agent.handlers import NonRetryableError, dispatch, registered_types  # noqa: E402
from agent.runner import Runner  # noqa: E402

TOKEN = "test-worker-token"


class FakeApi:
    """The lease endpoints, with just enough behaviour to exercise the runner."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queued: list[dict[str, Any]] = []
        self.completed: dict[str, dict[str, Any]] = {}
        self.failed: dict[str, dict[str, Any]] = {}
        self.heartbeats = 0
        self.claim_error: int | None = None
        self.complete_status = 200
        self.leased_jobs: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = {}
        if request.content:
            import json

            body = json.loads(request.content)
        self.calls.append((path, body))

        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Invalid worker token"})

        if path.endswith("/heartbeat"):
            self.heartbeats += 1
            return httpx.Response(200, json={"status": "ok", "serverTime": "2026-09-02T00:00:00Z"})

        if path.endswith("/jobs/claim"):
            if self.claim_error:
                return httpx.Response(self.claim_error, json={"detail": "boom"})
            handlers = body.get("handlers", [])
            taking = [j for j in self.queued if j["jobType"] in handlers][: body.get("max_jobs", 1)]
            for j in taking:
                self.queued.remove(j)
            return httpx.Response(200, json={"jobs": taking})

        if path.endswith("/complete"):
            job_id = path.split("/")[-2]
            if self.complete_status != 200:
                return httpx.Response(self.complete_status, json={"detail": "not yours"})
            self.completed[job_id] = body["result"]
            return httpx.Response(200, json={"status": "ok"})

        if path.endswith("/fail"):
            job_id = path.split("/")[-2]
            self.failed[job_id] = body
            return httpx.Response(200, json={"status": "queued", "attempts": 1})

        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "name": "desktop-test",
                    "status": "online",
                    "leasedJobs": list(self.leased_jobs),
                },
            )

        return httpx.Response(404, json={"detail": "no such route"})


def make_job(job_type: str = "noop", **payload: Any) -> dict[str, Any]:
    return {
        "id": f"job-{job_type}-{len(payload)}",
        "jobType": job_type,
        "payload": payload,
        "attempts": 1,
        "maxAttempts": 3,
        "leaseExpiresAt": "2026-09-02T01:00:00Z",
        "idempotencyKey": None,
    }


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def client(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> WorkerClient:
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(api.handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", factory)
    return WorkerClient("https://thedrop.channel", TOKEN)


def make_runner(client: WorkerClient) -> Runner:
    config = RunnerConfig(
        api_url="https://thedrop.channel",
        token=TOKEN,
        worker_name="desktop-test",
        handlers=("noop",),
        heartbeat_seconds=1,
        idle_poll_seconds=0,
        lease_seconds=900,
    )
    runner = Runner(config, client)
    # No GPU probing in tests; nvidia-smi may or may not exist on the runner's machine.
    runner._gpu_name, runner._gpu_vram = None, None
    return runner


# ------------------------------------------------------------------ config


def test_config_requires_https_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bearer token crosses the public internet; http:// would send it in clear."""
    monkeypatch.setenv("THEDROP_API_URL", "http://thedrop.channel")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)

    with pytest.raises(ConfigError, match="https"):
        load_config(("noop",))


def test_config_allows_plain_http_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEDROP_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)

    assert load_config(("noop",)).api_url == "http://127.0.0.1:8000"


def test_config_fails_loudly_on_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEDROP_API_URL", "https://thedrop.channel")
    monkeypatch.delenv("WORKER_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="WORKER_TOKEN"):
        load_config(("noop",))


# ------------------------------------------------------------------ handlers


def test_only_registered_handlers_are_advertised() -> None:
    """The claim call sends this list; advertising more would lease us undoable work."""
    assert registered_types() == ("noop",)


def test_dispatching_an_unregistered_type_is_not_retryable() -> None:
    with pytest.raises(NonRetryableError):
        dispatch("embed", {})


# ------------------------------------------------------------------ happy path


def test_claims_runs_and_completes_a_job(api: FakeApi, client: WorkerClient) -> None:
    api.queued.append(make_job("noop"))
    runner = make_runner(client)
    job = Job.from_api(api.queued[0] if api.queued else make_job())

    claimed = client.claim(["noop"], 1, 900)
    assert len(claimed) == 1

    runner._run_job(claimed[0])

    assert claimed[0].id in api.completed
    assert api.completed[claimed[0].id]["ok"] is True
    assert job.job_type == "noop"


def test_claim_only_returns_advertised_types(api: FakeApi, client: WorkerClient) -> None:
    api.queued.append(make_job("embed"))
    api.queued.append(make_job("noop"))

    claimed = client.claim(["noop"], 4, 900)

    assert [j.job_type for j in claimed] == ["noop"]


# ------------------------------------------------------------------ failure paths


def test_a_raising_handler_does_not_kill_the_runner(
    api: FakeApi, client: WorkerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.runner as runner_module

    def explode(_job_type: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("model server is down")

    monkeypatch.setattr(runner_module, "dispatch", explode)
    runner = make_runner(client)

    runner._run_job(Job.from_api(make_job("noop")))

    reported = next(iter(api.failed.values()))
    assert "model server is down" in reported["error"]
    # Transient by default: a model that is down now may be up in a minute.
    assert reported["retryable"] is True


def test_a_non_retryable_handler_error_is_reported_as_such(
    api: FakeApi, client: WorkerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.runner as runner_module

    def explode(_job_type: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise NonRetryableError("payload has no article id")

    monkeypatch.setattr(runner_module, "dispatch", explode)
    runner = make_runner(client)

    runner._run_job(Job.from_api(make_job("noop")))

    assert next(iter(api.failed.values()))["retryable"] is False


def test_a_lost_lease_discards_the_result(api: FakeApi, client: WorkerClient) -> None:
    """409 means the lease expired and someone else owns the job now.

    Re-applying our result would double-write whatever the job produces.
    """
    api.complete_status = 409
    runner = make_runner(client)

    runner._run_job(Job.from_api(make_job("noop")))

    assert api.completed == {}


def test_unreachable_api_is_survivable() -> None:
    """A dropped connection must surface as retryable, never as a crash.

    Deliberately does not use the `client` fixture: that fixture patches httpx.Client
    globally, which would replace this transport with the fake API's.
    """

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = WorkerClient.__new__(WorkerClient)
    client._client = httpx.Client(
        base_url="https://thedrop.channel",
        transport=httpx.MockTransport(refuse),
    )

    with pytest.raises(ApiUnavailableError):
        client.claim(["noop"], 1, 900)


def test_server_error_is_retryable_not_fatal(api: FakeApi, client: WorkerClient) -> None:
    api.claim_error = 503

    with pytest.raises(ApiUnavailableError):
        client.claim(["noop"], 1, 900)


def test_rejected_token_is_fatal(api: FakeApi) -> None:
    """Retrying a bad token forever looks exactly like being offline, but never heals."""
    real_client = httpx.Client
    bad = WorkerClient.__new__(WorkerClient)
    bad._client = real_client(
        base_url="https://thedrop.channel",
        transport=httpx.MockTransport(api.handler),
        headers={"Authorization": "Bearer wrong-token"},
    )

    with pytest.raises(AuthRejectedError):
        bad.claim(["noop"], 1, 900)


def test_runner_exits_2_when_the_token_is_rejected(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(api.handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", factory)
    runner = make_runner(WorkerClient("https://thedrop.channel", "wrong-token"))

    assert runner.run() == 2


# ------------------------------------------------------------------ shutdown


def test_stop_event_ends_the_loop(api: FakeApi, client: WorkerClient) -> None:
    """SIGTERM must drain rather than abandon leases mid-flight."""
    runner = make_runner(client)
    timer = threading.Timer(0.3, runner.stop_event.set)
    timer.start()
    try:
        assert runner.run() == 0
    finally:
        timer.cancel()


def test_startup_releases_leases_held_by_a_previous_run(
    api: FakeApi, client: WorkerClient
) -> None:
    """A restarted runner must hand back what its dead predecessor was holding.

    Worker identity is the token, not the process, so the new process IS the node that
    still owns those leases -- and its heartbeats refresh them. Left alone the job never
    expires (heartbeats keep extending) and never runs (this process does not know it
    exists). Observed live: 894s remaining on a 900s lease minutes after the runner
    holding it had been killed.
    """
    api.leased_jobs = ["orphan-1", "orphan-2"]
    runner = make_runner(client)

    runner._release_orphaned_leases()

    assert set(api.failed) == {"orphan-1", "orphan-2"}
    # Retryable: the work was never done, so it belongs back on the queue.
    assert all(f["retryable"] is True for f in api.failed.values())


def test_run_releases_orphans_before_heartbeating(api: FakeApi, client: WorkerClient) -> None:
    """`run()` must actually call the reconciliation, and call it FIRST.

    Testing the method alone would pass even if nobody invoked it -- and the first
    heartbeat extends exactly the stale leases we are trying to release, so ordering is
    part of the fix rather than a detail.
    """
    api.leased_jobs = ["orphan-1"]
    runner = make_runner(client)
    timer = threading.Timer(0.3, runner.stop_event.set)
    timer.start()
    try:
        runner.run()
    finally:
        timer.cancel()

    assert "orphan-1" in api.failed
    release_index = next(i for i, (p, _) in enumerate(api.calls) if p.endswith("/fail"))
    first_heartbeat = next(
        (i for i, (p, _) in enumerate(api.calls) if p.endswith("/heartbeat")), len(api.calls)
    )
    assert release_index < first_heartbeat


def test_startup_is_quiet_when_no_leases_are_held(api: FakeApi, client: WorkerClient) -> None:
    runner = make_runner(client)

    runner._release_orphaned_leases()

    assert api.failed == {}


def test_orphan_release_survives_an_unreachable_api(
    api: FakeApi, client: WorkerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconciliation that cannot run must not stop the runner from starting."""

    def unreachable() -> dict[str, Any]:
        raise ApiUnavailableError("connection refused")

    monkeypatch.setattr(client, "status", unreachable)
    runner = make_runner(client)

    runner._release_orphaned_leases()  # must not raise


def test_second_interrupt_exits_immediately(api: FakeApi, client: WorkerClient) -> None:
    """One Ctrl+C drains; the next must exit on that press, not the one after.

    The first version only restored the default handler and returned, so a second press
    did nothing and a third was needed -- which looks exactly like a hung process to
    whoever is pressing it.
    """
    import signal

    runner = make_runner(client)
    runner.install_signal_handlers()
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)

    try:
        handler(signal.SIGINT, None)
        assert runner.stop_event.is_set()

        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)


def test_heartbeat_reports_the_advertised_handlers(api: FakeApi, client: WorkerClient) -> None:
    runner = make_runner(client)

    runner._heartbeat_once()

    _path, body = next((c for c in api.calls if c[0].endswith("/heartbeat")), ("", {}))
    assert body["capabilities"]["handlers"] == ["noop"]
    assert body["status"] == "online"
