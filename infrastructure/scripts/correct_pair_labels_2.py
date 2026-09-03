"""Second round of one-time corrections to story-pair labels.

    uv run python infrastructure/scripts/correct_pair_labels_2.py --dry-run
    uv run python infrastructure/scripts/correct_pair_labels_2.py

After the first correction (correct_pair_labels.py, 4 pairs), the full remaining set of
23 same_event verdicts was read end to end -- not a sample this time. Ten did not hold
up:

  131 + 267  RFK Jr./measles vaccine doubt, vs an RFK Jr./Gov. Shapiro political
             dispute. Different specific things under one recurring name.

  89 + 90    A cost-of-living consultant interview, vs Trump's gas-price policy
             action. Related theme, no shared specific claim.

  31 + 141   The USPS whistleblower story (5 articles), vs a broad "Trump reshaping
             elections" survey piece that mentions USPS among several other threads.
             Same pattern as the first round's DOJ and Springfield/Guardian pairs.

  145 + 163  Trump telling Axios he isn't worried about a Russia/NATO attack, vs the
             CIA director floating a Trump-Putin-Zelensky summit. Nine shared entities
             (Trump, Putin, Ratcliffe, Russia, Ukraine, NATO...) made this look far
             stronger than the actual claims warrant -- different specific statements
             on the same broad Russia-Ukraine beat.

  25 + 67    Two NASA APOD posts: a lunar eclipse photo of the day, vs a monthly
             skywatching guide. Same recurring feature, not the same content.

  10 + 11    Two separate NPR segments on teen social-media policy generally -- no
             shared entity, thematic rather than event-specific.

  10 + 69    Same theme as above, paired with a PBS piece on social platforms' legal
             battles. Neither article extracted any usable entity at all.

  109 + 134  Iran-strikes retaliation news, vs an economic-impact ANALYSIS of the
             war. Genre shift (direct reporting vs. analysis), not the same event --
             the same distinction already correctly drawn for the AI-safety op-ed in
             the first labelling session.

  86 + 257   "China dissents" on a G20 finance-ministers agreement, vs Bessent
             rallying allies on Iran at the same summit. Same setting, different
             specific agenda item.

  84 + 87    A PBS "News Wrap" segment -- its own extracted entities include Apple and
             Lindsay Clancy alongside Iran-strike terms, meaning the article is ITSELF
             a digest bundling several unrelated stories. Pairing it with one story it
             mentions among several is the same trap the NPR "Up First" digest rule
             exists to prevent, in PBS's format instead of NPR's.

Thirteen pairs were read and left as same_event, including two (119+211, 103+211)
diagnostically useful on their own: both are the same vaccine-guidance announcement,
correctly judged as one event, and both were refused by the entity guard alone despite
0.86-0.93 similarity -- neither side's extraction produced a shared name. That is a real
recall cost with a real cause (weak extraction on short announcement-style articles),
distinct from every correction above, which is a labelling error, not a pipeline one.

Same reasoning as the first correction script for why this exists as a file rather than
a shell one-off, why it is idempotent, and why it is not a standing "--correct" feature:
see correct_pair_labels.py.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from thedrop_database import session_scope
from thedrop_database.label_recall import ordered_pair
from thedrop_database.models import StoryPairLabel
from thedrop_database.operator_env import load_operator_env

# (story_id, other_story_id, expected current verdict, corrected verdict).
CORRECTIONS: list[tuple[int, int, str, str]] = [
    (131, 267, "same_event", "different"),
    (89, 90, "same_event", "different"),
    (31, 141, "same_event", "different"),
    (145, 163, "same_event", "different"),
    (25, 67, "same_event", "different"),
    (10, 11, "same_event", "different"),
    (10, 69, "same_event", "different"),
    (109, 134, "same_event", "different"),
    (86, 257, "same_event", "different"),
    (84, 87, "same_event", "different"),
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
