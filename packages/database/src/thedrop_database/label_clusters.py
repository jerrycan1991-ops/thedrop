"""Label clustering decisions, so precision can be measured instead of argued about.

    python -m thedrop_database.label_clusters            # label what is unlabelled
    python -m thedrop_database.label_clusters --report   # just the numbers

Phase 3's exit criterion is precision >= 0.90 on a hand-labelled set of at least 200
articles. Reading eleven clusters by eye suggested roughly 0.8 and a pattern -- the
errors were all same-source companion pieces -- but eleven examples cannot choose
between the candidate fixes. Requiring cross-source would destroy correct clusters that
are six-sevenths one outlet; requiring two shared entities might destroy a correct pair
whose only shared entity is the town it happened in. A rule tuned on eleven examples
looks like an improvement whether or not it is one.

WHAT IS LABELLED: one row per PLACEMENT -- an article that JOINED a story. The founder
is not a placement. Nobody decided to put it there, so counting it would inflate
precision with decisions that were never taken.

`unsure` is recorded rather than skipped. An article a human could not judge is a fact
about the data, and dropping it biases the measurement towards the easy cases.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from thedrop_database import engine, session_scope
from thedrop_database.models import ClusterLabel, RawArticle, Source, Story, StorySource
from thedrop_database.operator_env import load_operator_env

VERDICTS = {"correct", "wrong", "unsure"}


@dataclass(frozen=True)
class Placement:
    story_id: int
    article_id: int
    similarity: float | None
    domain: str
    title: str


def parse_verdicts(answer: str, placements: list[Placement]) -> dict[int, str] | None:
    """Turn one typed answer into a verdict per placement, or None to quit.

    Accepts, for a story whose members are numbered from 1:

        y            every placement is correct
        n            every placement is wrong
        2            placement 2 is wrong, the rest are correct
        2,4          placements 2 and 4 are wrong
        s            unsure about all of them
        q            stop

    EVERY VERDICT NEEDS A KEYSTROKE. Enter on its own is not "yes" and never was
    accepted as one after the first labelling run produced 71 correct and 0 wrong --
    including three placements that had been independently flagged as questionable an
    hour earlier, and a stray "y" that arrived at the shell prompt after the tool had
    exited.

    The tool made that easy. Enter-as-yes was chosen so labelling two hundred articles
    would not be tedious, which on a measurement whose entire value is deliberateness
    made the fastest path through the tool also the one that records agreement.

    An unparseable answer returns an empty dict, which the caller treats as "ask again"
    -- never as a verdict. Guessing what someone meant is how ground truth gets
    corrupted.
    """
    answer = answer.strip().lower()
    if answer in {"q", "quit"}:
        return None
    if answer in {"y", "yes"}:
        return {p.article_id: "correct" for p in placements}
    if answer in {"n", "no"}:
        return {p.article_id: "wrong" for p in placements}
    if answer in {"s", "skip", "unsure"}:
        return {p.article_id: "unsure" for p in placements}

    wrong: set[int] = set()
    for part in answer.replace(" ", ",").split(","):
        if not part:
            continue
        if not part.isdigit():
            return {}
        index = int(part)
        if not 1 <= index <= len(placements):
            return {}
        wrong.add(index)
    if not wrong:
        return {}
    return {
        p.article_id: ("wrong" if i in wrong else "correct")
        for i, p in enumerate(placements, start=1)
    }


def precision(counts: dict[str, int]) -> float | None:
    """Correct placements over decided ones. `unsure` is excluded from the denominator.

    None when nothing has been decided -- reporting 0.0 or 1.0 for an empty set would
    be a number that looks like a measurement.
    """
    decided = counts.get("correct", 0) + counts.get("wrong", 0)
    return counts.get("correct", 0) / decided if decided else None


def _unlabelled_stories(db) -> list[int]:
    labelled = select(ClusterLabel.story_id)
    return list(
        db.scalars(
            select(StorySource.story_id)
            .where(StorySource.is_primary.is_(False), StorySource.story_id.not_in(labelled))
            .group_by(StorySource.story_id)
            .order_by(func.count(StorySource.id).desc(), StorySource.story_id)
        ).all()
    )


def _story_placements(db, story_id: int) -> tuple[str, list[Placement], Placement | None]:
    rows = db.execute(
        select(
            StorySource.raw_article_id,
            StorySource.similarity,
            StorySource.is_primary,
            Source.domain,
            RawArticle.title,
        )
        .join(RawArticle, RawArticle.id == StorySource.raw_article_id)
        .join(Source, Source.id == RawArticle.source_id)
        .where(StorySource.story_id == story_id)
        .order_by(StorySource.is_primary.desc(), StorySource.similarity)
    ).all()

    founder = None
    placements: list[Placement] = []
    for article_id, similarity, is_primary, domain, title in rows:
        item = Placement(
            story_id=story_id,
            article_id=article_id,
            similarity=float(similarity) if similarity is not None else None,
            domain=domain,
            title=title,
        )
        if is_primary:
            founder = item
        else:
            placements.append(item)

    story = db.get(Story, story_id)
    return (story.title if story else ""), placements, founder


def _counts(db) -> dict[str, int]:
    rows = db.execute(
        select(ClusterLabel.verdict, func.count(ClusterLabel.id)).group_by(ClusterLabel.verdict)
    ).all()
    return dict(rows)


def report() -> int:
    with session_scope() as db:
        counts = _counts(db)
        labelled_articles = db.scalar(select(func.count(ClusterLabel.id))) or 0

    print("labelled placements")
    for verdict in sorted(VERDICTS):
        print(f"  {verdict:<9} {counts.get(verdict, 0)}")

    score = precision(counts)
    print()
    if score is None:
        print("precision  not measurable yet (nothing decided)")
    else:
        print(f"precision  {score:.3f}  (target 0.90)")
    # The criterion counts ARTICLES, so say how far off the sample size is rather than
    # letting a confident-looking ratio over twelve placements pass for a measurement.
    print(f"sample     {labelled_articles} placements labelled (criterion wants >= 200 articles)")
    return 0


def label() -> int:
    who = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

    with session_scope() as db:
        story_ids = _unlabelled_stories(db)
        total_placements = (
            db.scalar(
                select(func.count(StorySource.id)).where(
                    StorySource.is_primary.is_(False),
                    StorySource.story_id.in_(story_ids) if story_ids else False,
                )
            )
            or 0
        )

    if not story_ids:
        print("nothing left to label")
        return report()

    print(f"{len(story_ids)} unlabelled stories, {total_placements} placements.")
    print("  y            every one is the same event as the founder")
    print("  n            none of them are")
    print("  2  or  1,3   those are wrong, the rest are right")
    print("  s            unsure")
    print("  q            stop (progress is kept)")
    print("There is no default; Enter on its own asks again.")
    print("")

    for story_id in story_ids:
        with session_scope() as db:
            title, placements, founder = _story_placements(db, story_id)
            if not placements:
                continue

            print(f"story {story_id}  {title[:72]}")
            if founder:
                print(f"     founder  {founder.domain:<22} {founder.title[:60]}")
            for index, item in enumerate(placements, start=1):
                score = "" if item.similarity is None else f"{item.similarity:.3f}"
                print(f"  {index}) {score:>8}  {item.domain:<22} {item.title[:60]}")

            while True:
                try:
                    answer = input("  same event? ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    return report()
                verdicts = parse_verdicts(answer, placements)
                if verdicts is None:
                    return report()
                if verdicts:
                    break
                print("  no default -- answer y, n, a number, s, or q")

            for item in placements:
                db.add(
                    ClusterLabel(
                        story_id=story_id,
                        raw_article_id=item.article_id,
                        verdict=verdicts[item.article_id],
                        labelled_by=who[:64],
                    )
                )
            print()

    return report()


def reset() -> int:
    """Delete every label, after an explicit confirmation typed in full.

    Labels are evidence. Deleting them is sometimes right -- a set produced faster than
    it was read is worse than no set, because it is the number that gets quoted later
    when deciding not to change something -- but it must be a deliberate act, not a
    flag someone tabs past.
    """
    with session_scope() as db:
        existing = db.scalar(select(func.count(ClusterLabel.id))) or 0

    if not existing:
        print("no labels to clear")
        return 0

    print(f"This deletes {existing} labels permanently.")
    try:
        answer = input("Type 'delete labels' to confirm: ")
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer.strip().lower() != "delete labels":
        print("not confirmed; nothing was deleted")
        return 1

    with session_scope() as db:
        db.query(ClusterLabel).delete()
    print(f"deleted {existing} labels")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="print the numbers and exit")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete every label, after typing a confirmation in full",
    )
    args = parser.parse_args(argv)

    loaded = load_operator_env()
    if loaded:
        print(f"(configuration from {loaded})")
        print("")

    try:
        if args.reset:
            return reset()
        return report() if args.report else label()
    except OperationalError:
        url = engine().url
        print(f"cannot connect to {url.host}:{url.port}/{url.database}", file=sys.stderr)
        if not os.environ.get("DATABASE_URL"):
            print("DATABASE_URL is not set. On the VPS:", file=sys.stderr)
            print("  set -a; . ~/.config/thedrop/thedrop.env; set +a", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
