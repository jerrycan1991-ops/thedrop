"""The labelling tool's judgement, which is the part that can corrupt ground truth.

Phase 3's exit criterion is precision >= 0.90 on a hand-labelled set. That measurement
is only worth having if the labels mean what the labeller intended, so the parsing is
tested harder than it looks like it deserves: a misread keystroke silently records the
opposite verdict, and nothing downstream can tell.

The database loop is not tested here. What matters is that an ambiguous answer is never
guessed at, and that precision over an empty set is not reported as a number.
"""

from __future__ import annotations

import pytest
from thedrop_database.label_clusters import Placement, parse_verdicts, precision


def placements(n: int) -> list[Placement]:
    return [
        Placement(story_id=1, article_id=100 + i, similarity=0.9, domain="x.invalid", title="t")
        for i in range(n)
    ]


@pytest.mark.parametrize("answer", ["y", "yes", "Y", "  y  ", ""])
def test_yes_marks_every_placement_correct(answer: str) -> None:
    """Empty counts as yes: most clusters are right, and a tool costing ten keystrokes
    per story does not get used for two hundred articles."""
    assert set(parse_verdicts(answer, placements(3)).values()) == {"correct"}


@pytest.mark.parametrize("answer", ["n", "no", "N"])
def test_no_marks_every_placement_wrong(answer: str) -> None:
    assert set(parse_verdicts(answer, placements(2)).values()) == {"wrong"}


def test_numbers_mark_only_those_wrong() -> None:
    verdicts = parse_verdicts("2", placements(3))
    items = placements(3)
    assert verdicts[items[0].article_id] == "correct"
    assert verdicts[items[1].article_id] == "wrong"
    assert verdicts[items[2].article_id] == "correct"


@pytest.mark.parametrize("answer", ["1,3", "1 3", "3,1"])
def test_several_numbers_are_accepted_in_any_form(answer: str) -> None:
    verdicts = parse_verdicts(answer, placements(3))
    items = placements(3)
    assert verdicts[items[0].article_id] == "wrong"
    assert verdicts[items[1].article_id] == "correct"
    assert verdicts[items[2].article_id] == "wrong"


def test_unsure_is_recorded_not_skipped() -> None:
    """An article a human could not judge is a fact about the data. Dropping it biases
    the measurement towards the easy cases."""
    assert set(parse_verdicts("s", placements(2)).values()) == {"unsure"}


def test_quit_is_distinguishable_from_a_verdict() -> None:
    assert parse_verdicts("q", placements(2)) is None


@pytest.mark.parametrize(
    "answer",
    ["maybe", "1x", "0", "4", "-1", "1,9", "?"],
    ids=["word", "mixed", "zero", "past the end", "negative", "one past", "punctuation"],
)
def test_an_answer_that_cannot_be_read_is_never_guessed(answer: str) -> None:
    """The one that protects the measurement. Interpreting "1x" as "1" would silently
    record a verdict the labeller did not give, and nothing downstream could detect it.
    An empty dict means ask again.
    """
    assert parse_verdicts(answer, placements(3)) == {}


# --------------------------------------------------------------------- precision


def test_precision_ignores_unsure() -> None:
    """`unsure` belongs in the record but not in the denominator: it is neither a
    success nor a failure of the clustering rule."""
    assert precision({"correct": 9, "wrong": 1, "unsure": 50}) == pytest.approx(0.9)


def test_precision_of_nothing_is_not_a_number() -> None:
    """Reporting 0.0 or 1.0 over an empty set would look like a measurement."""
    assert precision({}) is None
    assert precision({"unsure": 5}) is None


def test_precision_is_the_plain_ratio() -> None:
    assert precision({"correct": 8, "wrong": 2}) == pytest.approx(0.8)
