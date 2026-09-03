#!/usr/bin/env bash
#
# Register the verified feed set. Run on the VPS:
#
#   set -a; . ~/.config/thedrop/thedrop.env; set +a
#   bash infrastructure/scripts/add-feeds.sh
#
# One command rather than twenty `add_provider` invocations pasted into an SSH session,
# because long pastes get mangled by bracketed-paste markers and three separate checks
# were lost to it today.
#
# EVERY URL HERE WAS FETCHED AND PARSED BEFORE BEING LISTED. Of 27 candidates, 7 were
# unusable -- whitehouse.gov, census.gov and noaa.gov 404, dhs.gov and state.gov 403,
# epa.gov 405, and the CDC feed parsed but was empty. None of them are here. A feed that
# looks plausible is not a feed that works, and `add_provider` validates again on the
# way in, so a URL that rots later fails loudly rather than silently polling nothing.
#
# Re-running is safe: a slug that already exists is reported and skipped, not modified.
#
# POLL INTERVALS are set by how fast the source actually publishes, not by a default.
# A press-release feed that emits four items a day polled every ten minutes is 144
# wasted requests at someone who never agreed to serve them; a wire-speed feed polled
# hourly loses stories to the feed's own item limit.

set -Eeuo pipefail

cd "$(dirname "$0")/../.."

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set. Source the env first:" >&2
  echo "  set -a; . ~/.config/thedrop/thedrop.env; set +a" >&2
  exit 2
fi

added=0
skipped=0
failed=0

add() {
  local slug="$1" url="$2" name="$3" interval="$4"
  if uv run python -m thedrop_ingest.add_provider \
      --slug "$slug" --feed-url "$url" --name "$name" \
      --poll-interval "$interval" --enable >/tmp/add-feed.log 2>&1; then
    echo "  added    $slug (every ${interval}m)"
    added=$((added + 1))
  elif grep -qi "already exists" /tmp/add-feed.log; then
    echo "  exists   $slug"
    skipped=$((skipped + 1))
  else
    echo "  FAILED   $slug" >&2
    sed 's/^/             /' /tmp/add-feed.log >&2
    failed=$((failed + 1))
  fi
}

echo "General newsrooms"
add axios        "https://api.axios.com/feed/"                                             "Axios"            10
add guardian-us  "https://www.theguardian.com/us-news/rss"                                 "Guardian US"      10
add cnbc-top     "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114" "CNBC" 10
add the-hill     "https://thehill.com/news/feed/"                                          "The Hill"         15
add propublica   "https://www.propublica.org/feeds/propublica/main"                        "ProPublica"       60

echo "NPR sections"
# Barely overlap with the sections already registered: 0-1 shared items out of 10.
add npr-national  "https://feeds.npr.org/1003/rss.xml" "NPR National"  15
add npr-business  "https://feeds.npr.org/1006/rss.xml" "NPR Business"  15
add npr-science   "https://feeds.npr.org/1007/rss.xml" "NPR Science"   30
add npr-health    "https://feeds.npr.org/1128/rss.xml" "NPR Health"    30
add npr-education "https://feeds.npr.org/1013/rss.xml" "NPR Education" 60

echo "PBS NewsHour sections"
# These DO overlap the headlines feed already registered -- 3 to 12 of 20 items -- but
# each still brings 8 to 17 that the aggregate does not carry. The overlap costs a URL
# hash lookup, which is single-digit milliseconds and exactly what cheap dedup is for.
add pbs-politics "https://www.pbs.org/newshour/feeds/rss/politics" "PBS NewsHour Politics" 15
add pbs-nation   "https://www.pbs.org/newshour/feeds/rss/nation"   "PBS NewsHour Nation"   15
add pbs-world    "https://www.pbs.org/newshour/feeds/rss/world"    "PBS NewsHour World"    15
add pbs-economy  "https://www.pbs.org/newshour/feeds/rss/economy"  "PBS NewsHour Economy"  30
add pbs-science  "https://www.pbs.org/newshour/feeds/rss/science"  "PBS NewsHour Science"  30
add pbs-health   "https://www.pbs.org/newshour/feeds/rss/health"   "PBS NewsHour Health"   30

echo "Primary authorities"
# `.gov` sources are marked is_primary_authority at auto-creation, which is what lets a
# single one satisfy the corroboration rule for a claim about itself (CLAUDE.md). They
# publish slowly, so they are polled slowly.
add defense-news   "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20" "US Department of Defense" 30
add gao-reports    "https://www.gao.gov/rss/reports.xml"  "Government Accountability Office" 60
add uscourts-news  "https://www.uscourts.gov/news/rss"    "US Courts"                        60
add bls-news       "https://www.bls.gov/feed/bls_latest.rss" "Bureau of Labor Statistics"    60

echo
echo "added $added, already present $skipped, failed $failed"
[[ "$failed" -eq 0 ]]
