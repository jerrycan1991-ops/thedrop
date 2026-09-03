"""The NER model itself, on real hardware.

One assertion here carries the weight: two shootings in different states must share no
entity. That is the exact case PIPELINE.md §6 names when it calls entity overlap a
correctness guard rather than an optimization — embeddings put those two articles very
close together, and only the place names keep them apart.

The rest pin the precision bias the guard depends on. A missed entity costs a merge
that should have happened, which consolidation and a human can both fix. A false
entity that happens to match merges two unrelated events into one story asserting facts
about the wrong one, which nothing downstream can detect.

Skipped when the model stack is absent (`uv sync --group desktop-ml`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "agent-runner"))

from agent import entities  # noqa: E402

#: Only the tests that RUN the model need it. The surface-form repairs at the bottom
#: are pure functions and must keep running on a machine without torch -- they were
#: found in production output and are exactly what a CI run should protect.
_needs_model = pytest.mark.skipif(
    not entities.is_available(),
    reason="model stack not installed (uv sync --group desktop-ml)",
)
pytestmark = pytest.mark.gpu

OHIO = "A shooting in Dayton, Ohio left three dead. Police in Dayton said the suspect acted alone."
NEVADA = "A shooting in Reno, Nevada left three dead. Police in Reno said the suspect acted alone."


def names(text: str) -> set[str]:
    return {str(e["name"]) for e in entities.extract(text)}


@_needs_model
def test_two_shootings_in_different_states_share_no_entity() -> None:
    """The guard's whole reason for existing.

    These two sentences are near-identical in wording, so their embeddings sit close
    together and a similarity threshold alone would merge them into one story about a
    shooting that happened in two states.
    """
    overlap = names(OHIO) & names(NEVADA)

    assert overlap == set(), f"the guard would have merged two different events via {overlap}"


@_needs_model
def test_the_same_event_reported_twice_does_share_entities() -> None:
    """The guard must not block everything. A second account of the same event has to
    keep enough in common to clear it, or nothing ever clusters."""
    other_account = "Three people died in a Dayton shooting, Ohio officials confirmed."

    assert names(OHIO) & names(other_account)


@_needs_model
def test_people_organisations_and_places_are_typed() -> None:
    found = {
        str(e["name"]): str(e["type"])
        for e in entities.extract(
            "Jerome Powell said the Federal Reserve would hold rates. He spoke in Washington."
        )
    }

    assert found.get("Jerome Powell") == "PERSON"
    assert found.get("Federal Reserve") == "ORG"
    assert found.get("Washington") == "PLACE"


@_needs_model
def test_salience_ranks_the_repeatedly_mentioned_first() -> None:
    """Salience is centrality, not model confidence. A name mentioned once in passing
    should not gate a merge, however confidently it was tagged."""
    extracted = entities.extract(
        "Dayton police responded. Dayton officials spoke. Dayton residents gathered. "
        "A spokesman mentioned Cleveland once."
    )

    assert extracted[0]["name"] == "Dayton"
    assert extracted[0]["salience"] > 0.5


@_needs_model
def test_text_with_no_entities_returns_nothing_rather_than_noise() -> None:
    """An empty result is a valid outcome the VPS records as 'processed'. Returning
    junk here would put junk in the guard."""
    assert entities.extract("It rained heavily and the meeting was postponed.") == []


@_needs_model
def test_empty_text_does_not_reach_the_model() -> None:
    assert entities.extract("   ") == []


# ------------------------------------------------------- surface form repairs

# These need no model: `_clean` is a pure function. They live here because they were
# found by reading real extracted entities, and the strings are verbatim from that
# output.


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        ("##air Olajuwan Tidwell", "PERSON", "Olajuwan Tidwell"),
        ("##ine Ferris Pirro", "PERSON", "Ferris Pirro"),
        ("##l Andrew Green", "PERSON", "Andrew Green"),
    ],
    ids=["leading fragment", "split forename", "single letter"],
)
def test_wordpiece_fragments_are_dropped_not_kept(raw: str, kind: str, expected: str) -> None:
    """The tagger emits "##air Olajuwan Tidwell" when an entity begins mid-word, so its
    first token is half of one. Dropped rather than stripped to "air": half a first name
    is not a name, and two unrelated people could match on a shared fragment.
    """
    assert entities._clean(raw, kind) == expected


def test_an_entity_that_is_only_a_fragment_disappears() -> None:
    assert entities._clean("##only", "PERSON") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Washington, D. C", "Washington, D.C"), ("U. S", "United States")],
    ids=["initials after a comma", "the country"],
)
def test_split_initials_are_rejoined(raw: str, expected: str) -> None:
    """The aggregator rejoins "D.C." as "D. C". The period fix alone missed it, because
    the space follows a period rather than preceding one."""
    assert entities._clean(raw, "PLACE") == expected


@pytest.mark.parametrize(("raw", "expected"), [("Mo", "Missouri"), ("Wyo", "Wyoming")])
def test_state_abbreviations_expand_for_places(raw: str, expected: str) -> None:
    """Wire copy writes "Kansas City, Mo." A two-letter place that no other article
    spells the same way cannot license a join with anything."""
    assert entities._clean(raw, "PLACE") == expected


def test_state_abbreviations_do_not_expand_for_people() -> None:
    """Most are ordinary words or surnames. Expanding "Mass" wherever it appears would
    rewrite a person into a state, and give two unrelated stories a shared entity."""
    assert entities._clean("Mass", "PERSON") == "Mass"
