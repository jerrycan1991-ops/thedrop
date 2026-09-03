"""One-time correction: sources that were auto-created before `_NON_US_DOMAINS`
existed, and are still recorded under the "US" default despite being genuinely
headquartered elsewhere.

    uv run python infrastructure/scripts/correct_source_countries.py --dry-run
    uv run python infrastructure/scripts/correct_source_countries.py

`resolve_source` now consults a curated override (`thedrop_ingest.pipeline.
_NON_US_DOMAINS`) when auto-creating a source, but that override did not exist when
`theguardian.com` was first ingested -- it was created under the blind "US" default and
nothing has corrected it since. `sources.country` feeds directly into the US-relevance
"publisher share" signal (thedrop_database.scoring), so a wrong value there is not
cosmetic: it is exactly the kind of silently-wrong signal CLAUDE.md's "never fabricate"
rule exists to catch.

Idempotent: skips a domain if its country is not the value this script expects, so a
re-run (or a run after someone has already corrected it another way) changes nothing
further.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from thedrop_database import session_scope
from thedrop_database.models import Source
from thedrop_database.operator_env import load_operator_env
from thedrop_ingest.pipeline import _NON_US_DOMAINS

# Derived from the same override `resolve_source` uses for NEW sources, so this script
# can never drift out of sync with what auto-creation now does -- there is no second
# list to maintain by hand.
CORRECTIONS: list[tuple[str, str, str]] = [
    (domain, "US", country) for domain, country in _NON_US_DOMAINS.items()
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
        for domain, expected_from, corrected_to in CORRECTIONS:
            row = db.scalar(select(Source).where(Source.domain == domain))

            if row is None:
                print(f"  skip   {domain}: not in this database")
                continue
            if row.country != expected_from:
                print(
                    f"  skip   {domain}: country is {row.country!r}, "
                    f"not {expected_from!r} -- leaving it alone"
                )
                continue

            action = "would set" if args.dry_run else "set"
            print(f"  {action:<9} {domain}: country {expected_from!r} -> {corrected_to!r}")
            if not args.dry_run:
                row.country = corrected_to
                changed += 1

    if args.dry_run:
        print("\n(dry run -- nothing was changed)")
    else:
        print(f"\ncorrected {changed} of {len(CORRECTIONS)} sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
