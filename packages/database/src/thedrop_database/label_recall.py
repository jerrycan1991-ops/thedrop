"""Find joins that should have happened and did not.

    python -m thedrop_database.label_recall            # judge unlabelled pairs
    python -m thedrop_database.label_recall --report   # the numbers and the diagnosis

`cluster_labels` judges placements that HAPPENED. After 71 of them with zero errors,
that says the join threshold is not too loose — and nothing at all about whether it is
too strict, because it never looks at an article that failed to join. That blindness is
structural, and it points at the failure this design deliberately courts: over-splitting
is its chosen safe direction (ADR-0015), there are 289 single-article stories, and
consolidation has run every ten minutes since it shipped without merging anything.

So this asks the opposite question. For each single-article story it finds the nearest
story it did NOT join, shows both, and asks whether they are one event.

The number matters less than the DIAGNOSIS. Every pair is recorded with the similarity
and the shared-entity count at the moment it was judged, so a pair a human calls one
event says which condition kept them apart:

  * similarity below the join threshold  -> the threshold is too strict
  * no shared discriminative entity      -> the guard is too strict
  * both                                 -> they were never close to joining

Those imply completely different fixes, and without recording them the measurement would
produce a recall figure and no idea what to do about it.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError

from thedrop_database import engine, session_scope
from thedrop_database.clustering import (
    DEFAULT_JOIN_THRESHOLD,
    story_guard_entities,
)
from thedrop_database.models import (
    ClusterLabel,
    Entity,
    RawArticle,
    Source,
    Story,
    StoryEntity,
    StoryPairLabel,
    StorySource,
)
from thedrop_database.operator_env import load_operator_env

VERDICTS = {"same_event", "different", "unsure"}


@dataclass(frozen=True)
class Candidate:
    story_id: int
    other_id: int
    similarity: float
    shared_entities: int


def blocker(candidate: Candidate, join_threshold: float = DEFAULT_JOIN_THRESHOLD) -> str:
    """Which condition kept a pair apart. Only meaningful once a human says they match.

    Both conditions are required to join, so a pair can fail either or both. Reporting
    "threshold" for a pair that also shared no entity would send someone to tune a
    number that was never the binding constraint.
    """
    below = candidate.similarity < join_threshold
    unshared = candidate.shared_entities == 0
    if below and unshared:
        return "both"
    if below:
        return "threshold"
    if unshared:
        return "guard"
    # Neither blocked it, so something else did -- the 48h window, or the digest rule.
    return "other"


def parse_verdict(answer: str) -> str | None:
    """One typed answer to a verdict, "" to ask again, or None to quit.

    No default, for the reason the placement tool has none: a measurement whose value is
    deliberateness must not make agreement the cheapest keystroke.
    """
    answer = answer.strip().lower()
    if answer in {"q", "quit"}:
        return None
    if answer in {"y", "yes", "s"}:
        return "same_event"
    if answer in {"n", "no", "d"}:
        return "different"
    if answer in {"u", "unsure", "?"}:
        return "unsure"
    return ""


def recall(counts: dict[str, int], joined_correctly: int) -> float | None:
    """Joins made over joins that should have been made.

    The denominator is the correct joins already measured plus the missed ones found
    here. `unsure` is excluded from both, as in the placement tool: it is neither a
    success nor a failure of the rule.

    None when nothing has been judged -- a ratio over an empty set would look like a
    measurement.
    """
    missed = counts.get("same_event", 0)
    if joined_correctly + missed == 0:
        return None
    return joined_correctly / (joined_correctly + missed)


def _singleton_story_ids(db) -> list[int]:
    """Stories with exactly one article, oldest first, not yet judged."""
    labelled = select(StoryPairLabel.story_id).union(select(StoryPairLabel.other_story_id))
    return list(
        db.scalars(
            select(StorySource.story_id)
            .join(Story, Story.id == StorySource.story_id)
            .where(Story.merged_into_id.is_(None), StorySource.story_id.not_in(labelled))
            .group_by(StorySource.story_id)
            .having(func.count(StorySource.id) == 1)
            .order_by(StorySource.story_id)
        ).all()
    )


def nearest_other(db, story_id: int) -> Candidate | None:
    """The story most similar to this one that it is not already part of.

    Deliberately ignores the 48-hour window clustering uses. A pair that a human calls
    one event but which fell outside the window is a real miss and worth knowing about;
    filtering it out here would hide a failure mode by reusing the assumption under test.
    """
    story = db.get(Story, story_id)
    if story is None or story.centroid is None:
        return None

    distance = Story.centroid.cosine_distance(list(story.centroid))
    row = db.execute(
        select(Story.id, distance.label("distance"))
        .where(
            Story.id != story_id,
            Story.centroid.is_not(None),
            Story.merged_into_id.is_(None),
        )
        .order_by(distance)
        .limit(1)
    ).first()
    if row is None:
        return None

    other_id = int(row[0])
    shared = story_guard_entities(db, story_id) & story_guard_entities(db, other_id)
    return Candidate(
        story_id=story_id,
        other_id=other_id,
        similarity=1.0 - float(row[1]),
        shared_entities=len(shared),
    )


def _describe(db, story_id: int) -> list[str]:
    rows = db.execute(
        select(Source.domain, RawArticle.title)
        .join(StorySource, StorySource.raw_article_id == RawArticle.id)
        .join(Source, Source.id == RawArticle.source_id)
        .where(StorySource.story_id == story_id)
        .order_by(StorySource.is_primary.desc())
    ).all()
    return [f"{domain:<22} {title[:60]}" for domain, title in rows]


def ordered_pair(a: int, b: int) -> tuple[int, int]:
    """Lower id first, so one judgement is stored once however the pair is reached.

    Both directions occur in a single run: judging story 10 against its nearest
    neighbour 11 does not stop 11 coming up later with 10 as ITS nearest neighbour.
    """
    return (a, b) if a < b else (b, a)


def already_judged(db, a: int, b: int) -> bool:
    """Whether this pair has a verdict, checked at the moment of use.

    The candidate list is built once at startup, so it cannot know about pairs judged
    during the run. Relying on it alone crashed the first real session with a unique
    violation on (10, 11) -- and worse than the crash, it would have asked the same
    question twice.
    """
    low, high = ordered_pair(a, b)
    return (
        db.scalar(
            select(func.count(StoryPairLabel.id)).where(
                StoryPairLabel.story_id == low, StoryPairLabel.other_story_id == high
            )
        )
        or 0
    ) > 0


def _entity_names(db, story_id: int) -> list[str]:
    """Every entity on a story, marked with whether the guard would let it license a
    join. `-` means excluded: OTHER-typed, or too common to discriminate.
    """
    allowed = story_guard_entities(db, story_id)
    rows = db.execute(
        select(Entity.id, Entity.canonical_name, Entity.entity_type)
        .join(StoryEntity, StoryEntity.entity_id == Entity.id)
        .where(StoryEntity.story_id == story_id)
        .order_by(Entity.canonical_name)
    ).all()
    return [
        f"{'+' if entity_id in allowed else '-'} {name} ({kind})" for entity_id, name, kind in rows
    ]


def show_missed() -> int:
    """Every pair a human called one event, with what each side's entities were.

    "9 blocked by both" is a count, not a cause. Two articles about the same event that
    share no discriminative entity mean either the tagger missed the names or the two
    sides used different surface forms for the same thing -- and those have completely
    different fixes. Lowering the similarity threshold recovers NONE of the `both`
    cases, so reaching for it without reading these would be tuning the one lever that
    cannot help.
    """
    with session_scope() as db:
        pairs = db.execute(
            select(
                StoryPairLabel.story_id,
                StoryPairLabel.other_story_id,
                StoryPairLabel.similarity,
                StoryPairLabel.shared_entities,
            )
            .where(StoryPairLabel.verdict == "same_event")
            .order_by(StoryPairLabel.similarity.desc())
        ).all()

        if not pairs:
            print("no missed joins recorded yet")
            return 0

        for story_id, other_id, similarity, shared in pairs:
            candidate = Candidate(story_id, other_id, float(similarity or 0), int(shared or 0))
            print(
                f"stories {story_id} + {other_id}   similarity {float(similarity or 0):.3f}"
                f"   shared {shared}   blocked by: {blocker(candidate)}"
            )
            for side in (story_id, other_id):
                for line in _describe(db, side):
                    print(f"     {line}")
                names = _entity_names(db, side)
                print(f"       entities: {', '.join(names) if names else '(none)'}")
            print("")

    print("+ can license a join, - excluded (OTHER-typed, or too common)")
    return 0


def report() -> int:
    with session_scope() as db:
        rows = db.execute(
            select(
                StoryPairLabel.verdict,
                StoryPairLabel.similarity,
                StoryPairLabel.shared_entities,
            )
        ).all()
        # Correct joins already measured by the placement tool. Recall needs both
        # halves: joins made, over joins that should have been made.
        joined_correctly = (
            db.scalar(select(func.count(ClusterLabel.id)).where(ClusterLabel.verdict == "correct"))
            or 0
        )

    counts: dict[str, int] = {}
    blockers: dict[str, int] = {}
    for verdict, similarity, shared in rows:
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "same_event":
            candidate = Candidate(0, 0, float(similarity or 0), int(shared or 0))
            key = blocker(candidate)
            blockers[key] = blockers.get(key, 0) + 1

    print("judged pairs")
    for verdict in sorted(VERDICTS):
        print(f"  {verdict:<11} {counts.get(verdict, 0)}")

    missed = counts.get("same_event", 0)
    print()
    if missed:
        print("what kept the missed joins apart")
        for key in sorted(blockers):
            print(f"  {key:<11} {blockers[key]}")
        print()
    print(f"missed joins found: {missed} of {sum(counts.values())} pairs judged")
    score = recall(counts, joined_correctly)
    if score is None:
        print("recall     not measurable yet")
    else:
        print(f"recall     {score:.3f}   ({joined_correctly} joins made, {missed} missed)")
    return 0


def label(limit: int) -> int:
    who = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

    with session_scope() as db:
        story_ids = _singleton_story_ids(db)[:limit]

    if not story_ids:
        print("nothing left to judge")
        return report()

    print(f"{len(story_ids)} single-article stories to check against their nearest neighbour.")
    print("  y   these two are one event (a join was missed)")
    print("  n   different events (correctly kept apart)")
    print("  u   unsure")
    print("  q   stop (progress is kept)")
    print("There is no default; Enter on its own asks again.")
    print("")

    for story_id in story_ids:
        with session_scope() as db:
            candidate = nearest_other(db, story_id)
            if candidate is None:
                continue
            if already_judged(db, story_id, candidate.other_id):
                # Judged earlier in this same run, from the other side.
                continue

            print(f"story {story_id}")
            for line in _describe(db, story_id):
                print(f"     {line}")
            print(
                f"  nearest: story {candidate.other_id}   similarity {candidate.similarity:.3f}"
                f"   shared entities {candidate.shared_entities}"
            )
            for line in _describe(db, candidate.other_id):
                print(f"     {line}")

            while True:
                try:
                    answer = input("  one event? ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    return report()
                verdict = parse_verdict(answer)
                if verdict is None:
                    return report()
                if verdict:
                    break
                print("  no default -- answer y, n, u, or q")

            low, high = ordered_pair(story_id, candidate.other_id)
            # ON CONFLICT as well as the check above: the check is what stops the same
            # question being ASKED twice, this is what stops an answer being lost to a
            # crash if one slips through anyway.
            db.execute(
                pg_insert(StoryPairLabel)
                .values(
                    story_id=low,
                    other_story_id=high,
                    verdict=verdict,
                    similarity=round(candidate.similarity, 4),
                    shared_entities=candidate.shared_entities,
                    labelled_by=who[:64],
                )
                .on_conflict_do_nothing(constraint="uq_story_pair_labels_pair")
            )
            print("")

    return report()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="print the numbers and exit")
    parser.add_argument(
        "--missed",
        action="store_true",
        help="show every missed join with both sides' entities, to see WHY it was missed",
    )
    parser.add_argument(
        "--limit", type=int, default=40, help="how many singletons to offer (default 40)"
    )
    args = parser.parse_args(argv)

    loaded = load_operator_env()
    if loaded:
        print(f"(configuration from {loaded})")
        print("")

    try:
        if args.missed:
            return show_missed()
        return report() if args.report else label(args.limit)
    except OperationalError:
        url = engine().url
        print(f"cannot connect to {url.host}:{url.port}/{url.database}", file=sys.stderr)
        if not os.environ.get("DATABASE_URL"):
            print("DATABASE_URL is not set. On the VPS:", file=sys.stderr)
            print("  set -a; . ~/.config/thedrop/thedrop.env; set +a", file=sys.stderr)
        return 2
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
