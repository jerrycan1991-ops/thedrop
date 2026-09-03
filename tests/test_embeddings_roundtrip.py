"""Embeddings from the desktop back into the database (Phase 3, ADR-0005).

The pieces under test are the ones where a mistake is silent rather than loud:

  * vectors must reach `/worker/embeddings` and must NOT reach `jobs.result` -- that
    column is kept forever, so a leak there duplicates every embedding permanently;
  * they must be delivered BEFORE the job is completed, so a crash in between costs a
    recomputation rather than an article that nothing ever revisits;
  * a batch the server refuses (wrong model, wrong dimensions) must fail permanently.
    The generic client treats 4xx as "try later", which for a model mismatch would mean
    retrying forever while looking exactly like a network problem;
  * an article id must stay attached to its own vector. A reordering would mis-assign
    every embedding in the batch and nothing downstream could detect it.

Runs against httpx.MockTransport speaking the real protocol. No model, no GPU, no
services -- the encoder is stubbed, because what is being tested is the plumbing around
it, not sentence-transformers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "agent-runner"))

from agent import embedding, handlers  # noqa: E402
from agent.client import Job, PayloadRejectedError, WorkerClient  # noqa: E402
from agent.config import RunnerConfig  # noqa: E402
from agent.handlers import NonRetryableError  # noqa: E402
from agent.runner import Runner  # noqa: E402

TOKEN = "test-worker-token"
DIMENSIONS = 384


def unit_vector(seed: int = 1) -> list[float]:
    """A normalized 384-vector. The API rejects anything off the unit sphere."""
    vector = [0.0] * DIMENSIONS
    vector[seed % DIMENSIONS] = 1.0
    return vector


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stored: list[dict[str, Any]] = []
        self.completed: dict[str, dict[str, Any]] = {}
        self.failed: dict[str, dict[str, Any]] = {}
        self.embeddings_status = 200
        self.embeddings_error = "embedding model mismatch"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append(path)

        if path.endswith("/worker/embeddings"):
            if self.embeddings_status != 200:
                return httpx.Response(
                    self.embeddings_status, json={"detail": self.embeddings_error}
                )
            self.stored.append(body)
            return httpx.Response(200, json={"stored": len(body.get("items", [])), "unknown": []})
        if path.endswith("/complete"):
            self.completed[path.split("/")[-2]] = body["result"]
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/fail"):
            self.failed[path.split("/")[-2]] = body
            return httpx.Response(200, json={"status": "failed", "attempts": 1})
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"detail": "no such route"})


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


@pytest.fixture
def runner(client: WorkerClient) -> Runner:
    config = RunnerConfig(
        api_url="https://thedrop.channel",
        token=TOKEN,
        worker_name="desktop-test",
        handlers=("embed_articles",),
    )
    built = Runner(config, client)
    built._gpu_name, built._gpu_vram = None, None
    return built


@pytest.fixture
def stub_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """One distinct unit vector per text, in input order.

    Also puts `embed_articles` back in the registry. It unregisters itself when
    sentence-transformers is absent -- the ADR-0005 fail-safe, asserted separately in
    `test_embed_articles_is_unadvertised_without_the_model_stack`. These tests exercise
    the plumbing around the handler, so they need it present regardless of whether the
    machine running them has a GPU stack. `setitem` restores the original state on
    teardown, including its absence.
    """
    monkeypatch.setattr(
        embedding, "encode", lambda texts: [unit_vector(i + 1) for i in range(len(texts))]
    )
    monkeypatch.setattr(embedding, "model_name", lambda: "BAAI/bge-small-en-v1.5")
    monkeypatch.setitem(handlers._REGISTRY, "embed_articles", handlers.embed_articles)


def make_job(job_type: str = "embed_articles", **payload: Any) -> Job:
    return Job.from_api(
        {
            "id": "job-embed-1",
            "jobType": job_type,
            "payload": payload,
            "attempts": 1,
            "maxAttempts": 3,
            "idempotencyKey": "embed-v1-test",
        }
    )


# ------------------------------------------------------------------- handler


def test_the_handler_pairs_each_id_with_its_own_vector(stub_encoder: None) -> None:
    """By id, not by position downstream. A silent reordering would attach every
    article to the wrong vector and nothing could detect it afterwards."""
    result = handlers.embed_articles(
        {"items": [{"id": "a", "text": "first"}, {"id": "b", "text": "second"}]}
    )
    pairs = {item["id"]: item["vector"] for item in result["embeddings"]}
    assert pairs["a"] == unit_vector(1)
    assert pairs["b"] == unit_vector(2)
    assert result["model"] == "BAAI/bge-small-en-v1.5"


def test_a_vector_count_mismatch_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cannot happen with a sane encoder, and would mis-assign the whole batch if it
    did. Retrying the same input reproduces it exactly, so it must not be retryable."""
    monkeypatch.setattr(embedding, "encode", lambda texts: [unit_vector(1)])
    monkeypatch.setattr(embedding, "model_name", lambda: "BAAI/bge-small-en-v1.5")
    with pytest.raises(NonRetryableError):
        handlers.embed_articles({"items": [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]})


@pytest.mark.parametrize(
    "payload",
    [{}, {"items": []}, {"items": [{"id": "a"}]}, {"items": [{"text": "no id"}]}],
    ids=["no items key", "empty", "missing text", "missing id"],
)
def test_a_malformed_payload_is_not_retryable(payload: dict[str, Any]) -> None:
    with pytest.raises(NonRetryableError):
        handlers.embed_articles(payload)


def test_embed_articles_is_unadvertised_without_the_model_stack() -> None:
    """The fail-safe from ADR-0005: a desktop that cannot embed never claims embedding
    work, so batches wait for a capable runner instead of failing on an incapable one.

    This machine's actual state decides which branch is asserted -- pinning either would
    make the test lie on the other kind of machine.
    """
    advertised = handlers.registered_types()
    assert ("embed_articles" in advertised) == embedding.is_available()


# ------------------------------------------------------------------- delivery


def test_vectors_are_posted_and_never_reach_the_job_result(
    runner: Runner, api: FakeApi, stub_encoder: None
) -> None:
    """The core invariant. `jobs.result` is permanent; vectors in it would be a second
    copy of every embedding, forever."""
    runner._run_job(make_job(items=[{"id": "a", "text": "one"}]))

    assert len(api.stored) == 1
    assert api.stored[0]["model"] == "BAAI/bge-small-en-v1.5"
    assert api.stored[0]["items"] == [{"id": "a", "vector": unit_vector(1)}]

    completed = api.completed["job-embed-1"]
    assert "embeddings" not in completed
    assert completed["stored"] == 1


def test_vectors_are_delivered_before_the_job_is_completed(
    runner: Runner, api: FakeApi, stub_encoder: None
) -> None:
    """Completing first could mark a job done whose vectors were never stored, and
    nothing would ever revisit those articles."""
    runner._run_job(make_job(items=[{"id": "a", "text": "one"}]))

    order = [call for call in api.calls if call.endswith(("/embeddings", "/complete"))]
    assert order[0].endswith("/embeddings")
    assert order[1].endswith("/complete")


def test_a_refused_batch_fails_permanently(
    runner: Runner, api: FakeApi, stub_encoder: None
) -> None:
    """A 400 means the wrong model or the wrong dimensions. Recomputing produces the
    same rejected vectors, so retrying is not a recovery -- it is an infinite loop that
    looks like a network fault."""
    api.embeddings_status = 400

    runner._run_job(make_job(items=[{"id": "a", "text": "one"}]))

    assert "job-embed-1" not in api.completed
    failure = api.failed["job-embed-1"]
    assert failure["retryable"] is False
    assert "refused" in failure["error"]


def test_an_unreachable_api_leaves_the_job_leased(
    runner: Runner, api: FakeApi, stub_encoder: None
) -> None:
    """The work is done but undeliverable. Leave it leased: the lease expires, the
    reaper requeues, and the batch is simply embedded again."""
    api.embeddings_status = 503

    runner._run_job(make_job(items=[{"id": "a", "text": "one"}]))

    assert api.completed == {}
    assert api.failed == {}


def test_a_handler_returning_no_vectors_completes_normally(runner: Runner, api: FakeApi) -> None:
    """The delivery step is specific to embeddings and must not disturb anything else."""
    runner._run_job(make_job(job_type="noop"))

    assert api.stored == []
    assert api.completed["job-embed-1"]["ok"] is True


def test_the_client_reports_a_refusal_distinctly(client: WorkerClient, api: FakeApi) -> None:
    """PayloadRejectedError, not ApiUnavailableError. The distinction is the whole
    reason a rejected batch does not retry forever."""
    api.embeddings_status = 400
    api.embeddings_error = "expected 384 dimensions, got 768"

    with pytest.raises(PayloadRejectedError, match="384"):
        client.store_embeddings("BAAI/bge-small-en-v1.5", [{"id": "a", "vector": [0.0]}])
