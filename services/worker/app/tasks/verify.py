"""Cross-source verification dispatch (PIPELINE.md §11).

Runs ON THE VPS, inline -- like clustering (ADR-0015) and US relevance scoring
(ADR-0018), this needs the database and needs no model. See
thedrop_database.verification for what is and is not covered: the deterministic
subset only (authoritative/corroborated/single_source), not disputed/refuted.
"""

from __future__ import annotations

import logging

from thedrop_database import session_scope
from thedrop_database.verification import unverified_claim_ids, verify_claim

from app.celery_app import celery_app
from app.locks import dispatch_lock

logger = logging.getLogger(__name__)

#: Bounds a cold start the same way scoring's per-tick limit does: a large backlog of
#: newly-extracted claims must not make one tick do unbounded work.
MAX_PER_TICK = 500


@celery_app.task(name="app.tasks.verify.verify_claims_batch")
def verify_claims_batch() -> dict[str, object]:
    """Verify every claim that has not been through this stage yet.

    Under a lock, same reasoning as scoring: two overlapping ticks verifying the same
    claim twice would waste work, not corrupt anything (verification is idempotent --
    the second write just repeats the first), but the lock is cheap and consistent
    with the other dispatchers.
    """
    with dispatch_lock("verification") as acquired:
        if not acquired:
            return {"verified": 0, "status": "already_verifying"}

        with session_scope() as db:
            claim_ids = unverified_claim_ids(db, limit=MAX_PER_TICK)
            for claim_id in claim_ids:
                verify_claim(db, claim_id)

    if claim_ids:
        logger.info("verified claims", extra={"count": len(claim_ids)})
    return {"verified": len(claim_ids)}
