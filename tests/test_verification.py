"""Cross-source verification's decision rule (PIPELINE.md §11).

Only `compute_status` is tested here -- the pure function, no database. What has to
be right is the rule itself: an authoritative source wins outright, two sources only
corroborate when they are not the same syndicated copy, and everything else honestly
falls back to single_source rather than a status this stage cannot justify.
"""

from __future__ import annotations

from thedrop_database.verification import compute_status


def test_no_evidence_is_unverified() -> None:
    assert compute_status([]) == "unverified"


def test_one_source_is_single_source() -> None:
    assert compute_status([(1, False, b"hash-a")]) == "single_source"


def test_two_distinct_sources_with_different_content_corroborate() -> None:
    assert compute_status([(1, False, b"hash-a"), (2, False, b"hash-b")]) == "corroborated"


def test_two_sources_carrying_the_same_wire_copy_are_single_source() -> None:
    """ADR-0013: forty outlets carrying one wire story are forty sources and one
    witness. Two different source_ids with byte-identical content_hash must not
    corroborate -- it is the same account under two mastheads."""
    assert compute_status([(1, False, b"hash-a"), (2, False, b"hash-a")]) == "single_source"


def test_the_same_source_appearing_twice_does_not_corroborate() -> None:
    """Two evidence rows, one source_id -- e.g. two quotes from the same article.
    Distinct rows are not the same as distinct sources."""
    assert compute_status([(1, False, b"hash-a"), (1, False, b"hash-b")]) == "single_source"


def test_an_authoritative_source_wins_even_alone() -> None:
    assert compute_status([(1, True, b"hash-a")]) == "authoritative"


def test_an_authoritative_source_wins_over_corroboration() -> None:
    rows = [(1, True, b"hash-a"), (2, False, b"hash-b"), (3, False, b"hash-c")]
    assert compute_status(rows) == "authoritative"


def test_a_missing_content_hash_does_not_crash_and_does_not_corroborate_alone() -> None:
    """A null content_hash (should not happen in practice, but the column is
    nullable) must not be treated as "distinct content" against another null --
    two nulls are not evidence of two different articles."""
    assert compute_status([(1, False, None), (2, False, None)]) == "single_source"
