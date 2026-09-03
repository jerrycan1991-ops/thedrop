"""Shared dispatch helpers for the desktop job queues.

Both the embedding and extraction dispatchers need the same guarantee: a tick that runs
while the previous tick's jobs are still outstanding must not queue the same articles
again.

They originally got that by deriving `idempotency_key` from the batch's article ids and
relying on `ON CONFLICT DO NOTHING`. That was a misuse, and it broke the first backfill
attempted. `jobs.idempotency_key` exists so that COMPLETION is safe -- a job that
finished exactly as its lease expired cannot apply its result twice -- not to decide
what may be dispatched. Because the key was a pure function of the article ids, the
completed job rows blocked those articles from ever being queued again: clearing
`entities_extracted_at` to request a re-extraction produced `queued: 0` forever.

The guarantee belongs where the question actually is: **does this article already have
an unfinished job?** That is what `outstanding_article_ids` answers, and unlike a
content hash it stops being true once the work is done -- which is exactly the property
a backfill needs.

Concurrency between two overlapping ticks is handled by a lock in the task layer, where
Redis lives. This module stays database-only.
"""

from __future__ import annotations

import secrets

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from thedrop_database.enums import JobStatus
from thedrop_database.models import Job

#: Statuses that mean the work is still coming. A `done` or `failed` job says nothing
#: about whether the article should be processed again -- that is the marker column's
#: job, and conflating the two is what made backfills impossible.
UNFINISHED = (JobStatus.QUEUED, JobStatus.LEASED)


def outstanding_article_ids(db: Session, job_type: str) -> set[str]:
    """Public ids of articles already inside a queued or leased job of this type.

    One query for the whole queue rather than a containment test per article: the
    payloads are small and there are few unfinished jobs, so unpacking them is far
    cheaper than a JSONB lookup per candidate.
    """
    rows = db.execute(
        select(func.jsonb_array_elements(Job.payload["items"]).op("->>")(text("'id'"))).where(
            Job.job_type == job_type, Job.status.in_(UNFINISHED)
        )
    ).scalars()
    return {row for row in rows if row}


def new_batch_key(prefix: str) -> str:
    """A unique key per dispatch.

    Random on purpose. The key identifies THIS attempt so completion stays safe; it is
    deliberately not a fingerprint of the batch's contents, because a batch's contents
    recurring is a legitimate reason to dispatch again.
    """
    return f"{prefix}-{secrets.token_hex(8)}"
