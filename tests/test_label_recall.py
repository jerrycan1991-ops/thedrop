"""The recall check's judgement, and the diagnosis it exists to produce.

`cluster_labels` can only ever find the join threshold too loose — it never looks at an
article that failed to join. This tool asks the opposite question, and what makes it
worth building is not the ratio but the breakdown: for a pair a human calls one event,
WHICH condition kept them apart. "Too strict a threshold" and "too strict a guard" have
different fixes, and reporting the wrong one sends someone to tune a number that was
never binding.
"""

from __future__ import annotations

import pytest
from thedrop_database.label_recall import Candidate, blocker, parse_verdict, recall


def candidate(similarity: float, shared: int) -> Candidate:
    return Candidate(story_id=1, other_id=2, similarity=similarity, shared_entities=shared)


# ------------------------------------------------------------------ diagnosis


def test_a_pair_below_the_threshold_that_shares_an_entity_blames_the_threshold() -> None:
    assert blocker(candidate(0.70, 2), join_threshold=0.82) == "threshold"


def test_a_similar_pair_sharing_nothing_blames_the_guard() -> None:
    assert blocker(candidate(0.95, 0), join_threshold=0.82) == "guard"


def test_a_pair_failing_both_is_not_blamed_on_one() -> None:
    """The distinction that matters. Reporting "threshold" for a pair that also shared
    no entity would send someone to lower a number that was never the binding
    constraint, and the pair still would not join."""
    assert blocker(candidate(0.40, 0), join_threshold=0.82) == "both"


def test_a_pair_that_passed_both_was_stopped_by_something_else() -> None:
    """The 48-hour window and the digest rule can also refuse a join. Calling this
    "threshold" would be a lie about a rule that let it through."""
    assert blocker(candidate(0.95, 3), join_threshold=0.82) == "other"


# -------------------------------------------------------------------- verdicts


@pytest.mark.parametrize("answer", ["y", "yes", "s"])
def test_yes_means_a_join_was_missed(answer: str) -> None:
    assert parse_verdict(answer) == "same_event"


@pytest.mark.parametrize("answer", ["n", "no", "d"])
def test_no_means_correctly_kept_apart(answer: str) -> None:
    assert parse_verdict(answer) == "different"


def test_quit_is_distinguishable_from_a_verdict() -> None:
    assert parse_verdict("q") is None


@pytest.mark.parametrize("answer", ["", "  ", "maybe", "1"])
def test_there_is_no_default(answer: str) -> None:
    """Same reason as the placement tool: a measurement whose value is deliberateness
    must not make any answer the cheapest keystroke."""
    assert parse_verdict(answer) == ""


# ---------------------------------------------------------------------- recall


def test_recall_is_joins_made_over_joins_that_should_have_been() -> None:
    assert recall({"same_event": 10}, joined_correctly=90) == pytest.approx(0.9)


def test_unsure_and_different_do_not_enter_the_ratio() -> None:
    """`different` is a correct refusal, not a missed join, and belongs in neither
    side. Counting it would make a well-behaved run look like a failure."""
    assert recall({"same_event": 0, "different": 50, "unsure": 5}, 71) == pytest.approx(1.0)


def test_recall_of_nothing_is_not_a_number() -> None:
    assert recall({}, joined_correctly=0) is None
