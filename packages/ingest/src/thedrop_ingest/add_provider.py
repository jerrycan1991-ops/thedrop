"""Register an RSS/Atom feed as a provider.

Run on the VPS, where the database credentials live:

    python -m thedrop_ingest.add_provider --slug ap-top --feed-url https://example.com/rss
    python -m thedrop_ingest.add_provider --slug ap-top --feed-url ... --enable
    python -m thedrop_ingest.add_provider --slug ap-top --update --poll-interval 30

**The feed is fetched and parsed before the row is written.** A URL that 404s, returns
HTML, or carries a DOCTYPE fails here in a second, with the reason. The alternative is
discovering it later as five consecutive failures and a tripped circuit breaker, which
tells you something is wrong but not what.

New providers are created **disabled** unless `--enable` is passed. Nothing polls a feed
you have not looked at.

This lives in thedrop_ingest rather than thedrop_database because validation needs the
RSS adapter, and thedrop_ingest already depends on thedrop_database -- the other
direction would be a cycle.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from thedrop_database import session_scope
from thedrop_database.enums import CircuitState
from thedrop_database.models import Provider

from thedrop_ingest.providers import ProviderError
from thedrop_ingest.providers.rss import RSSProvider

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("add-provider")

ADAPTER_CLASS = "thedrop_ingest.providers.rss.RSSProvider"

#: How far back the validation fetch looks. Only used to decide what to show the
#: operator; the row's real first poll uses pipeline.FIRST_RUN_LOOKBACK.
_VALIDATION_LOOKBACK = timedelta(days=7)


def validate_feed(slug: str, feed_url: str) -> int:
    """Fetch and parse the feed. Returns the item count, or raises ProviderError."""
    adapter = RSSProvider(slug=slug, feed_url=feed_url)
    try:
        page = adapter.fetch(datetime.now(UTC) - _VALIDATION_LOOKBACK, None)
    finally:
        adapter.close()

    logger.info("feed OK: %d item(s) in the last 7 days", len(page.items))
    for item in page.items[:3]:
        estimated = " [timestamp estimated]" if item.timestamp_estimated else ""
        logger.info("  - %s%s", item.title[:90], estimated)
    if page.skipped:
        logger.info("  (%d entr(ies) skipped: %s)", len(page.skipped), page.skipped[0][1])

    # An empty feed is not an error -- a quiet publisher is still a valid source -- but
    # it is worth saying out loud, because it is indistinguishable from a wrong URL
    # until something eventually arrives.
    if not page.items:
        logger.warning("feed parsed but returned no recent items; check the URL is right")
    return len(page.items)


def add_provider(
    slug: str,
    feed_url: str | None,
    name: str | None,
    enable: bool,
    poll_interval: int | None,
    update: bool,
    skip_validation: bool,
    rescan: bool = False,
) -> int:
    with session_scope() as db:
        provider = db.scalar(select(Provider).where(Provider.slug == slug))

        if provider is not None and not update:
            logger.error("provider %r already exists. Use --update to change it.", slug)
            return 1
        if provider is None and update:
            logger.error("no provider %r to update.", slug)
            return 1

        target_url = feed_url or (provider.config or {}).get("feed_url") if provider else feed_url
        if not target_url:
            logger.error("--feed-url is required when creating a provider")
            return 2

        if not skip_validation:
            try:
                validate_feed(slug, target_url)
            except ProviderError as exc:
                logger.error("feed validation failed: %s", exc)
                logger.error("no row was written")
                return 1

        if provider is None:
            provider = Provider(
                slug=slug,
                display_name=name or slug,
                adapter_class=ADAPTER_CLASS,
                enabled=enable,
                config={"feed_url": target_url},
                poll_interval_minutes=poll_interval or 15,
                circuit_state=CircuitState.CLOSED,
            )
            db.add(provider)
            action = "created"
        else:
            provider.config = {**(provider.config or {}), "feed_url": target_url}
            if name:
                provider.display_name = name
            if poll_interval:
                provider.poll_interval_minutes = poll_interval
            if enable:
                provider.enabled = True
                # An operator re-enabling a provider means "try again", so a stale open
                # circuit from a previous outage must not silently keep it idle.
                provider.circuit_state = CircuitState.CLOSED
                provider.circuit_opened_at = None
                provider.consecutive_failures = 0
            if rescan:
                # Clearing the timestamps sends the next poll back through
                # FIRST_RUN_LOOKBACK instead of the six-hour overlap.
                #
                # A provider gets exactly one wide backlog window, and it is consumed by
                # the first poll that SUCCEEDS -- storing nothing still counts. So a feed
                # enabled while the window was too narrow for its publishing cadence can
                # never reach its own backlog again, and the items are not late, they are
                # unreachable. Without this the only remedy is hand-editing the row.
                provider.last_success_at = None
                provider.last_error_at = None
                provider.cursor = None
            action = "updated"

        # Read the row's ACTUAL state before the session closes, not the flag that was
        # passed in. `--update` without `--enable` leaves `enabled` untouched, so
        # reporting the flag told an operator their provider was disabled when it was
        # polling normally -- and pointed them at a fix they did not need.
        is_enabled = bool(provider.enabled) if provider is not None else enable

    logger.info("provider %s: %s (%s)", action, slug, "enabled" if is_enabled else "disabled")
    if not is_enabled:
        logger.info("nothing will poll it until you re-run with --enable")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="Stable identifier, e.g. ap-top")
    parser.add_argument("--feed-url", help="RSS or Atom URL")
    parser.add_argument("--name", help="Display name for the admin. Defaults to the slug.")
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Poll this feed. Providers are created disabled without it.",
    )
    parser.add_argument("--poll-interval", type=int, help="Minutes between polls. Default 15.")
    parser.add_argument("--update", action="store_true", help="Modify an existing provider.")
    parser.add_argument(
        "--rescan",
        action="store_true",
        help=(
            "Forget when this provider was last polled, so the next poll uses the full "
            "first-run window again. For a feed enabled while that window was too "
            "narrow to reach its backlog."
        ),
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Write the row without fetching the feed. For a feed that is temporarily down.",
    )
    args = parser.parse_args()

    return add_provider(
        slug=args.slug,
        feed_url=args.feed_url,
        name=args.name,
        enable=args.enable,
        poll_interval=args.poll_interval,
        update=args.update,
        skip_validation=args.skip_validation,
        rescan=args.rescan,
    )


if __name__ == "__main__":
    sys.exit(main())
