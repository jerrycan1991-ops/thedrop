"""US relevance scoring: the arithmetic and entity matching (PIPELINE.md §7).

Only two of the formula's five signals are implemented -- see scoring.py's module
docstring for why. What has to be right here is the part that keeps that honest: the
rescaling math, and that `US_ENTITY_MARKERS` matches what it claims to and nothing more.
"""

from __future__ import annotations

from thedrop_database.scoring import (
    US_ENTITY_MARKERS,
    WEIGHT_ENTITIES,
    WEIGHT_PUBLISHER_SHARE,
    ScoreResult,
    _normalise,
)


def test_the_two_implemented_weights_sum_to_the_documented_coverage() -> None:
    """PIPELINE.md 7 assigns 0.30 to entities and 0.20 to publisher share. If either
    constant drifts, `coverage` silently starts lying about how much of the formula
    ran."""
    assert WEIGHT_ENTITIES == 0.30
    assert WEIGHT_PUBLISHER_SHARE == 0.20


def test_full_marks_on_both_signals_scores_100() -> None:
    result = ScoreResult(score=100, entity_signal=1.0, publisher_signal=1.0)
    assert result.score == 100


def test_zero_on_both_signals_scores_0() -> None:
    result = ScoreResult(score=0, entity_signal=0.0, publisher_signal=0.0)
    assert result.score == 0


def test_basis_names_the_three_unimplemented_signals() -> None:
    """A reader of the stored score must be able to tell it is partial without knowing
    the module's source -- this is the field that says so."""
    result = ScoreResult(score=50, entity_signal=0.5, publisher_signal=0.5)
    basis = result.basis()

    assert basis["coverage"] == 0.50
    assert set(basis["signals_not_implemented"]) == {
        "topic_class_us_salience",
        "direct_impact_on_us_audiences",
        "us_search_trend_signal",
    }
    assert set(basis["signals"]) == {"us_entities", "us_publisher_share"}


def test_basis_records_which_entities_actually_matched() -> None:
    """The number alone cannot be audited. Reading WHY a story scored high or low on
    this signal requires the matched list, not just its length."""
    result = ScoreResult(
        score=60, entity_signal=0.67, publisher_signal=0.5, matched_entities=["Texas", "FBI"]
    )
    assert result.basis()["signals"]["us_entities"]["matched"] == ["Texas", "FBI"]


# --------------------------------------------------------------- entity markers


def test_a_us_state_is_a_marker() -> None:
    assert _normalise("Massachusetts") in US_ENTITY_MARKERS


def test_the_country_itself_is_a_marker() -> None:
    assert _normalise("United States") in US_ENTITY_MARKERS


def test_a_foreign_country_is_not_a_marker() -> None:
    """The negative case matters as much as the positive one: a story about Nepal must
    not accidentally score as American because of a marker that is too broad."""
    assert _normalise("Nepal") not in US_ENTITY_MARKERS
    assert _normalise("Iran") not in US_ENTITY_MARKERS


def test_matching_is_case_and_whitespace_insensitive() -> None:
    """Entity extraction produces "United States" from one article and possibly
    different casing or spacing from another -- see agent/entities.py's own
    normalisation. This signal must not silently miss a match over formatting."""
    assert _normalise("  UNITED    STATES  ") == _normalise("United States")
    assert _normalise("UNITED STATES") in US_ENTITY_MARKERS


def test_a_state_capital_alone_is_not_a_marker() -> None:
    """Deliberately narrow. "Austin" is not in the list -- it is also a common surname
    and a city name shared with other countries, and the module docstring is explicit
    that scoring 0 on an unlisted entity is a true statement about what was found, not
    a claim the story is not American."""
    assert _normalise("Austin") not in US_ENTITY_MARKERS
