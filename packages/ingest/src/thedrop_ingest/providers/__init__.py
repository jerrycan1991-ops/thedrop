"""Provider adapters.

Each implements `NewsProvider` (PIPELINE.md 2). Nothing downstream imports one of
these modules -- the pipeline consumes `NormalizedItem` and nothing else -- so adding a
provider cannot change the pipeline, and a broken adapter cannot corrupt anything
beyond its own page.
"""

from thedrop_ingest.providers.base import (
    MAX_ITEMS_PER_RUN,
    MAX_RESPONSE_BYTES,
    NewsProvider,
    ProviderError,
    ProviderHealth,
    ProviderPage,
    ResponseTooLargeError,
    read_capped,
)
from thedrop_ingest.providers.rss import RSSProvider

__all__ = [
    "MAX_ITEMS_PER_RUN",
    "MAX_RESPONSE_BYTES",
    "NewsProvider",
    "ProviderError",
    "ProviderHealth",
    "ProviderPage",
    "RSSProvider",
    "ResponseTooLargeError",
    "read_capped",
]
