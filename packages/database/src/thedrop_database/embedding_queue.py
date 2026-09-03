"""Turn un-embedded articles into work orders for the desktop.

The VPS computes no embeddings (ADR-0005). It selects what needs one, packs the text
into a job payload, and waits for the 4070 to post vectors back.

Why the payload carries TEXT rather than just ids: the runner holds no database
credentials (ADR-0001), so it cannot look anything up. A work order that is not
self-describing would need a second endpoint and a second round trip to become one.
The text is truncated here rather than on the desktop, so the payload size is bounded
by a decision the VPS makes and a handler cannot widen.

Source text is untrusted (ADR-0008), and stays untrusted here. Nothing in this path
interprets it -- it is tokenised into a vector, which is not a channel through which an
instruction can act.
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

#: bge-small-en-v1.5 truncates at 512 tokens. Sending much more than that is paid for
#: in payload size and thrown away by the tokeniser, so it is cut here.
MAX_TEXT_CHARS = 1500

#: Job type. Must match the handler the runner registers, or the API will never lease
#: these to anyone -- see agent/handlers.py.
JOB_TYPE = "embed_articles"


def _text_for(article: RawArticle) -> str:
    """Title plus lede, which is what the vector should represent.

    Deliberately NOT the full body: an embedding of 4000 words of boilerplate is
    dominated by the boilerplate, and clustering then groups articles by publisher
    furniture rather than by subject.
    """
    parts = [article.title or "", article.dek or article.body_text or ""]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())[:MAX_TEXT_CHARS]


def pending_embedding_count(db: Session) -> int:
    return db.scalar(select(func.count(RawArticle.id)).where(RawArticle.embedding.is_(None))) or 0


def enqueue_embedding_batches(
    db: Session,
    *,
    batch_size: int = 32,
    max_batches: int = 8,
    priority: int = 0,
) -> list[str]:
    """Queue up to `max_batches` embedding jobs. Returns the idempotency keys queued.

    `max_batches` bounds a single tick: on a cold start with a large backlog, queueing
    every batch at once would put thousands of rows in front of a desktop that may not
    even be online, and starve anything else behind them.
    """
    rows = db.scalars(
        select(RawArticle)
        .where(RawArticle.embedding.is_(None))
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
                # Nothing to embed. Left alone rather than marked done: a title-less row
                # is an ingestion defect, and silently consuming it here would hide it.
                continue
            items.append({"id": str(article.public_id), "text": text})

        if not items:
            continue

        key = new_batch_key("embed-v1")
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
        logger.info("queued embedding batches", extra={"count": len(queued)})
    return queued
