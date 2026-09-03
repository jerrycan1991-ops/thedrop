"""Turn un-extracted articles into entity-extraction work orders for the desktop.

Same shape as `embedding_queue`, and deliberately a separate module rather than a
parameter on that one: the two select different rows, bound their payloads differently,
and will diverge further when extraction starts reading the body rather than the lede.

Selection is on `entities_extracted_at IS NULL`, not on "has no entity rows". An
article containing no recognisable entities is a legitimate outcome, and the two are
indistinguishable without the timestamp -- so the marker is what stops such an article
being re-queued on every tick forever.

Source text is untrusted (ADR-0008) and stays untrusted here. It is tagged, not
interpreted; the output is a list of surface strings, which is not a channel through
which an instruction can act.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from thedrop_database.dispatch import new_batch_key, outstanding_article_ids
from thedrop_database.models import Job, RawArticle

logger = logging.getLogger(__name__)

#: More context than the embedding payload gets. Entities appear throughout a piece --
#: the town in paragraph one, the official named in paragraph six -- and the tagger has
#: no 512-token ceiling to make the rest wasted, since it windows internally.
MAX_TEXT_CHARS = 4000

JOB_TYPE = "extract_entities"


def _text_for(article: RawArticle) -> str:
    parts = [article.title or "", article.dek or "", article.body_text or ""]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())[:MAX_TEXT_CHARS]


def pending_extraction_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count(RawArticle.id)).where(RawArticle.entities_extracted_at.is_(None))
        )
        or 0
    )


def enqueue_extraction_batches(
    db: Session,
    *,
    batch_size: int = 16,
    max_batches: int = 8,
    priority: int = 0,
) -> list[str]:
    """Queue up to `max_batches` extraction jobs. Returns the idempotency keys queued.

    Batches are smaller than embedding's: each item carries more text, and a token
    classifier costs more per article than an encoder does.
    """
    rows = db.scalars(
        select(RawArticle)
        .where(RawArticle.entities_extracted_at.is_(None))
        .order_by(RawArticle.discovered_at, RawArticle.id)
        .limit(batch_size * max_batches)
    ).all()

    # Articles already inside a queued or leased job. Excluded here rather than
    # deduplicated by a content-hashed key, which is what made backfills impossible:
    # a completed job's key blocked its articles from ever being queued again.
    outstanding = outstanding_article_ids(db, JOB_TYPE)
    rows = [r for r in rows if str(r.public_id) not in outstanding]

    queued: list[str] = []
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        items: list[dict[str, Any]] = []
        for article in chunk:
            text = _text_for(article)
            if not text:
                # Nothing to tag. Left for the ingestion defect it is, rather than
                # marked extracted -- which would hide it behind a plausible-looking
                # "processed, found nothing".
                continue
            items.append({"id": str(article.public_id), "text": text})

        if not items:
            continue

        key = new_batch_key("entities-v1")
        result = db.execute(
            pg_insert(Job)
            .values(
                job_type=JOB_TYPE,
                payload={"items": items},
                priority=priority,
                idempotency_key=key,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Job.id)
        )
        if result.scalar() is not None:
            queued.append(key)

    if queued:
        logger.info("queued entity extraction batches", extra={"count": len(queued)})
    return queued
