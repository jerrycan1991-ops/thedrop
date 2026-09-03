"""What the pipeline has actually done, in one command.

    python -m thedrop_database.pipeline_status
    python -m thedrop_database.pipeline_status --clusters

Exists because checking it otherwise meant pasting a multi-line `python -c` into an SSH
session, and terminals mangle long pastes -- bracketed-paste markers (`^[[200~`) end up
in the command and it dies with a syntax error. Three separate checks were lost that
way. A short command name cannot be mangled.

Read-only. It opens one connection, runs counting queries and prints. Safe to run
against production, which under ADR-0012 is the only database there is.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from thedrop_database import engine
from thedrop_database.operator_env import load_operator_env

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

#: `merged_into_id is null` matches `unscored_story_ids` in scoring.py -- an
#: absorbed story has nothing left to score, so it must not count as backlog here.
_SCORING = """
select
  count(*)                                                as stories,
  count(*) filter (where us_relevance_score is not null)  as scored,
  round(avg(us_relevance_score)
        filter (where us_relevance_score is not null))::int as avg_score,
  min(us_relevance_score) as min_score,
  max(us_relevance_score) as max_score
from stories
where merged_into_id is null
"""

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


#: Multi-article stories with their members. Counts say whether clustering is
#: degenerate; only the titles say whether it is RIGHT, and precision is the exit
#: criterion. Reading a dozen clusters is the cheapest honest check there is.
_CLUSTERS = """
select s.id,
       s.title            as story_title,
       count(*) over (partition by s.id) as members,
       src.domain,
       ra.title           as article_title,
       ss.similarity
from story_sources ss
join stories s      on s.id = ss.story_id
join raw_articles ra on ra.id = ss.raw_article_id
join sources src     on src.id = ra.source_id
where s.id in (
  select story_id from story_sources group by story_id having count(*) > 1
)
order by members desc, s.id, ss.similarity nulls first
"""


def show_clusters() -> int:
    """Print every story with more than one article, and what is in it."""
    with engine().connect() as conn:
        rows = conn.execute(text(_CLUSTERS)).all()

    if not rows:
        print("no multi-article stories yet")
        return 0

    current = None
    for row in rows:
        if row.id != current:
            current = row.id
            print("")
            print(f"story {row.id}  ({row.members} articles)  {row.story_title[:70]}")
        # The similarity that justified the join, as recorded at the time. It cannot be
        # recomputed later -- the centroid has moved since.
        score = "founder" if row.similarity is None else f"{float(row.similarity):.3f}"
        print(f"  {score:>8}  {row.domain:<28} {row.article_title[:64]}")
    return 0


def _explain_connection_failure(exc: OperationalError) -> int:
    """Say what went wrong instead of printing forty lines of driver traceback.

    Missing `DATABASE_URL` is the overwhelmingly likely cause on the VPS: settings fall
    back to a built-in localhost default, there is no Postgres on localhost there, and
    the resulting stack trace names psycopg rather than the actual problem. This command
    exists to be the easy operational check, so failing unhelpfully defeats it.
    """
    url = engine().url
    print(f"cannot connect to {url.host}:{url.port}/{url.database}", file=sys.stderr)
    # Keyed on the env var alone, not on the host. An unset DATABASE_URL is the
    # actionable cause wherever it happens; adding a host check only creates a case
    # where the advice is withheld precisely when it would have helped.
    if not os.environ.get("DATABASE_URL"):
        print("", file=sys.stderr)
        print(
            "DATABASE_URL is not set, so this fell back to the local default. "
            "On the VPS, source the environment first:",
            file=sys.stderr,
        )
        print("  set -a; . ~/.config/thedrop/thedrop.env; set +a", file=sys.stderr)
    else:
        print(f"  {exc.orig}", file=sys.stderr)
    return 2


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
        merged = conn.execute(
            text("select count(*) from stories where merged_into_id is not null")
        ).scalar()
        # Merges are invisible otherwise: the story count simply goes down, which reads
        # as stories never having existed rather than as consolidation working.
        print(f"  merged away        {merged}")

        scoring = conn.execute(text(_SCORING)).one()
        print("")
        print("us relevance scoring")
        print(f"  scored             {scoring.scored} / {scoring.stories}")
        if scoring.scored:
            print(
                f"  score range        {scoring.min_score}-{scoring.max_score} "
                f"(avg {scoring.avg_score})"
            )
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clusters",
        action="store_true",
        help="list every multi-article story and its members, for judging precision",
    )
    args = parser.parse_args()
    # Before anything touches the database: a command typed into an SSH session has
    # no environment of its own. Reported rather than silent -- loading a credential
    # file should be visible in the output that follows.
    loaded = load_operator_env()
    if loaded:
        print(f"(configuration from {loaded})")
        print("")

    try:
        code = show_clusters() if args.clusters else main()
        # `print()` to a pipe is buffered, not written immediately -- most calls just
        # fill a userspace buffer. Without this, a `| head` closing early was often
        # not caught here at all: the actual EPIPE only happened later, when the
        # interpreter flushed that buffer during shutdown, which is AFTER this try
        # block has already exited via sys.exit()'s SystemExit and can no longer catch
        # anything. Flushing explicitly, inside the try, forces the write -- and the
        # broken pipe it raises -- to happen where it is actually catchable.
        sys.stdout.flush()
        sys.exit(code)
    except OperationalError as exc:
        sys.exit(_explain_connection_failure(exc))
    except BrokenPipeError:
        # A perfectly normal `| head -40`. Redirect to devnull before exiting so
        # Python's own shutdown flush has nothing left to fail on.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
