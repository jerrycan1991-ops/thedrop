"""One-time correction to four story-pair labels from the second recall session.

    uv run python infrastructure/scripts/correct_pair_labels.py --dry-run
    uv run python infrastructure/scripts/correct_pair_labels.py

The session that produced 27 same_event verdicts in 104 pairs was reviewed jointly
(operator + assistant) by reading back the actual headlines and entities the labelling
tool had shown. Four did not hold up:

  78 + 210   Springfield's Haitian community facing TPS-related deportation, vs a
             Guardian piece on the administration deporting "Black and brown people"
             broadly across Afghanistan, Haiti and South Africa. A specific local story
             against a broad investigative piece that uses it as one data point among
             several -- not the same event.

  120 + 162  "AI agents are hacking systems without human input", vs the OpenAI/Hugging
             Face investigation and "AI labs are facing an agent control problem".
             Three pieces on one THEME (AI agent safety), no shared incident.

  93 + 146   Tariffs' effect on Michigan midterms, vs which states tariffs hit hardest.
             Same policy, no shared specific claim -- one is a ground-level midterms
             story, the other a state-by-state data breakdown.

  63 + 125   An NPR digest ("ICE says it has enough body cameras... And, Congress
             averts...") vs a PBS piece on ICE surveillance concerns generally. The
             digest bundles several stories; the PBS piece is not about the
             body-camera claim specifically.

74 + 188 (Apple's CEO transition plus two Tim Cook retrospectives) was reviewed
alongside these and left as same_event: the retrospectives were published in the same
cycle specifically because of the transition.

Recorded here rather than run as a one-off shell command, so the correction and its
reasoning are both in git history rather than lost to a terminal scrollback -- the same
principle `cluster_labels` and `story_pair_labels` themselves are built on: labels are
evidence, and evidence that cannot be traced back to a reason is not much better than a
guess.

Deliberately NOT a general "--correct" flag on label_recall.py. A one-time fix does not
need to become a permanent feature, and a standing correction path invites correcting
labels casually instead of reading them carefully the first time.

Idempotent: skips a pair if it is not in the state this script expects, so a re-run (or
a run against a database that was reset since) changes nothing further.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from thedrop_database import session_scope
from thedrop_database.label_recall import ordered_pair
from thedrop_database.models import StoryPairLabel
from thedrop_database.operator_env import load_operator_env

# (story_id, other_story_id, expected current verdict, corrected verdict). Ids as shown
# in the --missed output; ordered_pair() normalises which one is "low".
CORRECTIONS: list[tuple[int, int, str, str]] = [
    (78, 210, "same_event", "different"),
    (120, 162, "same_event", "different"),
    (93, 146, "same_event", "different"),
    (63, 125, "same_event", "different"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change; apply nothing"
    )
    args = parser.parse_args()

    loaded = load_operator_env()
    if loaded:
        print(f"(configuration from {loaded})\n")

    changed = 0
    with session_scope() as db:
        for story_id, other_id, expected_from, corrected_to in CORRECTIONS:
            low, high = ordered_pair(story_id, other_id)
            row = db.execute(
                select(StoryPairLabel).where(
                    StoryPairLabel.story_id == low, StoryPairLabel.other_story_id == high
                )
            ).scalar_one_or_none()

            if row is None:
                print(f"  skip   {low}+{high}: no label found (already corrected, or reset)")
                continue
            if row.verdict != expected_from:
                print(
                    f"  skip   {low}+{high}: verdict is {row.verdict!r}, "
                    f"not {expected_from!r} -- leaving it alone"
                )
                continue

            action = "would set" if args.dry_run else "set"
            print(f"  {action:<9} {low}+{high}: {expected_from!r} -> {corrected_to!r}")
            if not args.dry_run:
                row.verdict = corrected_to
                changed += 1

    if args.dry_run:
        print("\n(dry run -- nothing was changed)")
    else:
        print(f"\ncorrected {changed} of {len(CORRECTIONS)} labels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
