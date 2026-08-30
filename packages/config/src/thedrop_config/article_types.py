"""Article types, loaded from the canonical JSON definition.

Article types are a **closed set** that business logic switches on, so unlike
categories they are version-controlled rather than stored as runtime rows. The single
definition lives in ``article_types.json`` next to this module; TypeScript imports the
same file. Neither language re-declares the list.

Why constants rather than a ``StrEnum``: the enum was referenced in exactly one line of
production code, and building an enum dynamically from JSON costs static analysis and
autocomplete for no benefit. Plain frozensets and a generated SQL helper are simpler
and do everything the enum did here.

Source-of-truth hierarchy for article types:
  1. ``article_types.json``  -- canonical, version-controlled
  2. this module / TypeScript -- derived at import time, never edited by hand
  3. the database CHECK constraint -- enforcement, generated from (1)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

_DEFINITION_PATH: Final = Path(__file__).parent / "article_types.json"

_raw: Final[dict[str, Any]] = json.loads(_DEFINITION_PATH.read_text(encoding="utf-8"))

#: Editorial types, in declaration order. Keys are the values stored in
#: ``articles.article_type``.
ARTICLE_TYPES: Final[dict[str, dict[str, Any]]] = dict(_raw["editorial"])

#: Commercial (affiliate) article formats.
COMMERCIAL_TYPES: Final[dict[str, dict[str, Any]]] = dict(_raw["commercial"])

#: The editorial types that may never carry an affiliate link, CTA or product
#: placement. Order preserved from the JSON because it is rendered into SQL.
EDITORIAL_ARTICLE_TYPES_ORDERED: Final[tuple[str, ...]] = tuple(
    name for name, spec in ARTICLE_TYPES.items() if spec.get("forbidsCommercial")
)

EDITORIAL_ARTICLE_TYPES: Final[frozenset[str]] = frozenset(EDITORIAL_ARTICLE_TYPES_ORDERED)

ALL_ARTICLE_TYPES: Final[frozenset[str]] = frozenset(ARTICLE_TYPES) | frozenset(COMMERCIAL_TYPES)

DEFAULT_ARTICLE_TYPE: Final[str] = "NEWS"


def commercial_forbidden_sql(column: str = "article_type") -> str:
    """Render the CHECK expression that keeps commercial content out of editorial types.

    Generated from the canonical list rather than hand-written, so adding a type that
    forbids commercial content updates the constraint definition automatically.

    The output must remain byte-identical to what is already applied in the database
    (revision ``bf45495a0cae``), or Alembic will detect drift and demand a migration.
    ``tests/test_invariants.py`` asserts the exact string, and ``alembic check`` proves
    the database agrees.
    """
    values = ", ".join(f"'{name}'" for name in EDITORIAL_ARTICLE_TYPES_ORDERED)
    return f"{column} NOT IN ({values})"


def is_editorial(article_type: str) -> bool:
    """True when the type forbids affiliate links, CTAs and product placement."""
    return article_type in EDITORIAL_ARTICLE_TYPES


def is_known(article_type: str) -> bool:
    return article_type in ALL_ARTICLE_TYPES
