"""What the pipeline has actually done, in one command.

    python -m thedrop_database.pipeline_status

Exists because checking it otherwise meant pasting a multi-line `python -c` into an SSH
session, and terminals mangle long pastes -- bracketed-paste markers (`^[[200~`) end up
in the command and it dies with a syntax error. Three separate checks were lost that
way. A short command name cannot be mangled.

Read-only. It opens one connection, runs counting queries and prints. Safe to run
against production, which under ADR-0012 is the only database there is.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from thedrop_database import engine

#: Ordered oldest-stage-first, so the row where the numbers stop moving is the stage
#: that is stuck.
_STAGES = """
select
  count(*)                                                    as ingested,
  count(*) filter (where embedding is not null)               as embedded,
  count(*) filter (where entities_extracted_at is not null)   as extracted,
  count(*) filter (where story_id is not null)                as clustered
from raw_articles
"""

_JOBS = """
select job_type, status, count(*) as n
from jobs
group by job_type, status
order by job_type, status
"""

_TOP_ENTITIES = """
select e.canonical_name, e.entity_type, count(*) as n
from entities e
join raw_article_entities r on r.entity_id = e.id
group by e.canonical_name, e.entity_type
order by n desc, e.canonical_name
limit 10
"""

_STORIES = "select status, count(*) as n from stories group by status order by status"

#: The honest measure of clustering. Every story holding one article means the guard is
#: refusing everything; a handful of stories holding all of them means it is refusing
#: nothing. Both are failures, and the counts distinguish them at a glance.
_CLUSTER_SHAPE = """
select
  count(*)                            as stories,
  coalesce(sum(members), 0)           as clustered_articles,
  coalesce(max(members), 0)           as largest,
  count(*) filter (where members = 1) as singletons
from (
  select story_id, count(*) as members
  from story_sources
  group by story_id
) s
"""


def main() -> int:
    with engine().connect() as conn:
        stages = conn.execute(text(_STAGES)).one()
        print("raw_articles")
        print(f"  ingested   {stages.ingested}")
        print(f"  embedded   {stages.embedded}")
        print(f"  extracted  {stages.extracted}")
        print(f"  clustered  {stages.clustered}")

        print("\njobs")
        rows = conn.execute(text(_JOBS)).all()
        if not rows:
            print("  none")
        for row in rows:
            print(f"  {row.job_type:<20} {row.status:<10} {row.n}")

        print("\nstories")
        rows = conn.execute(text(_STORIES)).all()
        if not rows:
            print("  none")
        for row in rows:
            print(f"  {row.status:<20} {row.n}")

        shape = conn.execute(text(_CLUSTER_SHAPE)).one()
        print("")
        print("clustering")
        print(f"  stories            {shape.stories}")
        print(f"  articles in them   {shape.clustered_articles}")
        print(f"  largest story      {shape.largest}")
        print(f"  single-article     {shape.singletons}")
        print("\ntop entities")
        rows = conn.execute(text(_TOP_ENTITIES)).all()
        if not rows:
            print("  none")
        for row in rows:
            # The names here are the honest check on extraction quality. Recognisable
            # people, agencies and places mean the guard has something to work with;
            # fragments and stopwords mean the confidence threshold or the surface
            # cleaning needs attention before clustering is built on top of it.
            print(f"  {row.n:>4}  {row.entity_type:<8} {row.canonical_name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
