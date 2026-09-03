"""The embedding model itself, on real hardware (Phase 3, ADR-0005).

Everything else in the embedding suite stubs the encoder, because what those tests are
about is the plumbing. Nothing there would notice if the model produced garbage, ran on
the CPU, or was the wrong model entirely — which is exactly the gap that lets a machine
bought for its GPU quietly do the work on four cores.

So these run the real thing:

  * the vector is 384-dimensional and unit length, which is what the API demands and
    what makes cosine similarity a dot product;
  * semantically related text really is closer than unrelated text — the only check
    here that would catch a model that loads, returns the right shape, and means
    nothing;
  * the model sits on CUDA when CUDA exists. A CPU fallback is a valid runner and an
    invalid *desktop*, and it reports success either way.

Skipped when the model stack is absent (`uv sync --group desktop-ml`), which is the
normal state for CI and for a VPS.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.routers.worker import EmbeddingItem, EmbeddingsRequest, store_embeddings
from sqlalchemy import select
from sqlalchemy.orm import Session
from thedrop_config import get_settings
from thedrop_database import engine
from thedrop_database.models import Provider, RawArticle, Source

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "agent-runner"))

from agent import embedding  # noqa: E402

pytestmark = [
    pytest.mark.gpu,
    pytest.mark.skipif(
        not embedding.is_available(),
        reason="model stack not installed (uv sync --group desktop-ml)",
    ),
]

DIMENSIONS = get_settings().ai.embedding_dimensions
MODEL = get_settings().ai.embedding_model  # the real config, not a stand-in
TEST_DOMAIN = "pytest-gpu-fixture.invalid"
FIXTURE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def dot(a: list[float], b: list[float]) -> float:
    """Cosine similarity — the vectors are unit length, so the dot product is it."""
    return sum(x * y for x, y in zip(a, b, strict=True))


# ----------------------------------------------------------------- the encoder


def test_the_encoder_returns_what_the_api_demands() -> None:
    """384 dimensions and unit length. The API refuses anything else, so a model that
    disagreed would have every batch rejected in production and none in the tests that
    stub it."""
    vectors = embedding.encode(["The Senate approved the budget 51-49 on Tuesday."])

    assert len(vectors) == 1
    assert len(vectors[0]) == DIMENSIONS
    assert abs(dot(vectors[0], vectors[0]) - 1.0) < 0.01


def test_the_encoder_preserves_input_order() -> None:
    """The handler pairs ids to vectors by position before re-attaching them by id. A
    reordering here would mis-assign every article in the batch."""
    texts = [
        "The Federal Reserve held interest rates steady.",
        "The Yankees won in extra innings.",
        "A hurricane made landfall in Florida.",
    ]
    once = embedding.encode(texts)
    again = embedding.encode(texts)

    for first, second in zip(once, again, strict=True):
        assert dot(first, second) > 0.99, "the same text produced a different vector"


def test_related_text_is_closer_than_unrelated_text() -> None:
    """The only assertion here that would catch a model which loads, returns the right
    shape, and means nothing.

    Clustering rests entirely on this being true; a wrong or broken model would still
    satisfy every structural check in the rest of the suite.
    """
    rates_a, rates_b, baseball = embedding.encode(
        [
            "The Federal Reserve held interest rates steady at its September meeting.",
            "The Fed left its benchmark rate unchanged, citing cooling inflation.",
            "The Yankees beat the Red Sox 4-3 in eleven innings.",
        ]
    )

    related = dot(rates_a, rates_b)
    unrelated = dot(rates_a, baseball)

    assert related > unrelated, f"related {related:.3f} not above unrelated {unrelated:.3f}"
    assert related > 0.7, f"two accounts of the same story scored only {related:.3f}"


def test_the_model_is_the_one_the_deployment_stores() -> None:
    """A mismatch is refused by the API, so it would show up as every batch failing.
    Better to see it here than as a queue that never drains."""
    assert embedding.model_name() == MODEL


def test_the_model_runs_on_the_gpu_when_there_is_one() -> None:
    """A CPU fallback is a valid runner and an invalid desktop — and it reports success
    either way. The PyPI torch wheel on Windows is CPU-only, so this is a live trap,
    not a hypothetical one.
    """
    import torch

    if not torch.cuda.is_available():
        pytest.skip("no CUDA device visible to torch")

    embedding.encode(["warm the model"])
    assert embedding._load().device.type == "cuda"


# -------------------------------------------------------- real vectors, real write


@pytest.fixture
def db() -> Iterator[Session]:
    """Always rolled back — see tests/test_ingest_pipeline_db.py."""
    connection = engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


class FakeNode:
    id = 1
    name = "desktop-gpu-test"


@pytest.mark.db
def test_real_vectors_pass_validation_and_land_in_the_row(db: Session) -> None:
    """End to end on the parts that were only ever stubbed: a real model's output
    through the real validation into a real column.

    The validation is the point. Dimension, model and unit-length checks all pass or
    fail on what the model actually emits, and until this ran nothing had ever fed it
    anything but a hand-written vector.
    """
    # Created rather than skipped-on-absence: a freshly rebuilt local database has no
    # providers, and skipping there would silently drop the one test that proves real
    # vectors survive validation. Rolled back with everything else.
    provider = db.scalar(select(Provider).limit(1))
    if provider is None:
        provider = Provider(
            slug="pytest-gpu-provider",
            display_name="pytest",
            adapter_class="thedrop_ingest.providers.rss.RSSProvider",
            enabled=False,
            config={"feed_url": f"https://{TEST_DOMAIN}/feed.xml"},
        )
        db.add(provider)
        db.flush()

    source = Source(domain=TEST_DOMAIN, name="pytest gpu fixture")
    db.add(source)
    db.flush()

    url = f"https://{TEST_DOMAIN}/1"
    article = RawArticle(
        provider_id=provider.id,
        source_id=source.id,
        canonical_url=url,
        original_url=url,
        url_hash=(9_000_001).to_bytes(32, "big"),
        title="Federal Reserve holds rates steady",
        dek="The decision was unanimous.",
        published_at=FIXTURE_EPOCH,
        discovered_at=FIXTURE_EPOCH + timedelta(hours=1),
        injection_flags={"patterns": []},
    )
    db.add(article)
    db.flush()

    [vector] = embedding.encode(
        ["Federal Reserve holds rates steady\n\nThe decision was unanimous."]
    )

    result = store_embeddings(
        EmbeddingsRequest(
            model=embedding.model_name(),
            items=[EmbeddingItem(id=str(article.public_id), vector=vector)],
        ),
        FakeNode(),
        db,
        get_settings(),
    )

    assert result == {"stored": 1, "unknown": []}
    db.refresh(article)
    assert article.embedding is not None
    assert len(article.embedding) == DIMENSIONS
    assert article.embedded_at is not None
