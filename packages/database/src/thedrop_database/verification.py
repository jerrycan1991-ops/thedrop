"""Cross-source verification: turning extraction's raw claims into a checkable status
(PIPELINE.md §11).

Implemented so far: the three outcomes computable from data already in
`claim_evidence` and `sources`, with no model call and no new column:

  * `authoritative` -- at least one evidence article's source has
    `is_primary_authority` set (`.gov`/`.mil`/`.gov.uk`/`.europa.eu`, populated at
    ingestion in `thedrop_ingest.pipeline.resolve_source`).
  * `corroborated` -- at least two DISTINCT sources, whose evidence articles are not
    all the same underlying wire copy. Distinctness is checked two ways: `source_id`
    (who published it) and `raw_articles.content_hash` (what the article actually
    says) -- ADR-0013 is explicit that source identity alone cannot license an
    independence claim, because "forty outlets carrying one wire story are forty
    sources and one witness." A claim whose only two sources ran byte-identical body
    text is one witness under a second masthead, not two.
  * `single_source` -- everything else that has at least one evidence row: one source,
    or multiple sources that turned out to be the same syndicated copy.

NOT implemented: `disputed` and `refuted`. Both require deciding whether two
DIFFERENTLY-WORDED claims about the same fact actually conflict -- a semantic
judgement, not something derivable from source counts. PIPELINE.md §11 itself calls
for Opus-tier verification with an independent second pass for exactly this, and
CLAUDE.md's "never fabricate" rule is the reason this module does not reach for a
keyword-overlap heuristic instead: a status this stage cannot actually justify would
be worse than the honest `unverified`/`single_source`/`corroborated` it leaves in
place. See ADR-0022.

Also NOT implemented: the "reliability >= threshold" clause PIPELINE.md's
corroboration rule specifies. Every source in this corpus sits at
`reliability_score`'s default (0.400) -- nothing in this codebase has ever actively
computed it, since PIPELINE.md §9's per-source reliability needs a correction-rate
history from PUBLISHED articles, which do not exist yet. Gating on a number nothing
has ever computed would make `corroborated` unreachable for the entire corpus, which
is a worse outcome than not checking it.

Runs on the VPS, not the desktop PIPELINE.md §11 tags it for: this slice is pure SQL
and needs no model, the same reasoning ADR-0015 and ADR-0018 already established for
clustering and US relevance scoring. The deferred disputed/refuted piece, whenever it
is built, is where a model and the desktop actually belong.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from thedrop_database.enums import VerificationStatus
from thedrop_database.models import Claim, ClaimEvidence, RawArticle, Source


def _evidence_signals(db: Session, claim_id: int) -> list[tuple[int, bool, bytes | None]]:
    """(source_id, is_primary_authority, content_hash) per evidence row for a claim."""
    return list(
        db.execute(
            select(ClaimEvidence.source_id, Source.is_primary_authority, RawArticle.content_hash)
            .join(Source, Source.id == ClaimEvidence.source_id)
            .join(RawArticle, RawArticle.id == ClaimEvidence.raw_article_id)
            .where(ClaimEvidence.claim_id == claim_id)
        ).all()
    )


def compute_status(rows: list[tuple[int, bool, bytes | None]]) -> str:
    """Pure function: evidence rows in, a `VerificationStatus` out.

    Kept separate from the DB-touching caller so the decision rule itself -- the part
    that actually matters and the part most likely to need a unit test that does not
    need Postgres -- is testable with plain tuples.
    """
    if not rows:
        return VerificationStatus.UNVERIFIED

    if any(is_authority for _, is_authority, _ in rows):
        return VerificationStatus.AUTHORITATIVE

    distinct_sources = {source_id for source_id, _, _ in rows}
    distinct_content = {h for _, _, h in rows if h is not None}
    if len(distinct_sources) >= 2 and len(distinct_content) >= 2:
        return VerificationStatus.CORROBORATED

    return VerificationStatus.SINGLE_SOURCE


def verify_claim(db: Session, claim_id: int) -> str:
    """Compute and store one claim's verification_status. Returns the new status."""
    status = compute_status(_evidence_signals(db, claim_id))
    db.execute(
        update(Claim)
        .where(Claim.id == claim_id)
        .values(verification_status=status, verified_at=datetime.now(UTC))
    )
    db.flush()
    return status


def unverified_claim_ids(db: Session, *, limit: int) -> list[int]:
    """Claims never run through this stage, oldest first.

    `verification_status != UNVERIFIED` is itself the marker -- unlike extraction's
    `_extracted_at IS NULL` columns, no separate timestamp is needed, because
    UNVERIFIED already means exactly "not yet processed" by this enum's own
    definition (DATABASE.md), and every outcome this module can currently produce
    moves a claim off it.
    """
    return list(
        db.scalars(
            select(Claim.id)
            .where(Claim.verification_status == VerificationStatus.UNVERIFIED)
            .order_by(Claim.id)
            .limit(limit)
        ).all()
    )
