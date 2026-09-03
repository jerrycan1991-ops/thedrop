"""Named-entity recognition, on the desktop only.

This exists to serve one requirement, and its design follows from it. PIPELINE.md §6
will not let two articles cluster together unless they share a salient entity, because
embeddings alone happily merge "shooting in Ohio" with "shooting in Nevada". That is a
correctness guard, so what matters here is PRECISION, not coverage:

  * a missed entity means two articles do not join -- over-splitting, which the
    consolidation pass and a human can both see and fix;
  * a wrong entity that happens to match means two different events become one story
    asserting facts about the wrong one, which nothing downstream can detect.

Those costs are not symmetric, so low-confidence predictions are discarded rather than
kept as weak signal.

`dslim/bert-base-NER` rather than spaCy: transformers and torch are already installed
for embeddings, so this adds a model download and no new runtime. Its four labels map
onto the coarse end of `EntityType` -- PER, ORG, LOC, MISC. The richer types the schema
allows (EVENT, LEGISLATION, PRODUCT) are not produced here, and are left for the
story-level extraction pass that needs them; a guard only needs to know that Ohio is
not Nevada.

Loaded lazily and unregistered when absent, exactly as `agent.embedding` is, so a
desktop without the model stack never claims work it cannot do.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from collections import Counter

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "dslim/bert-base-NER"

#: Below this the prediction is dropped. High on purpose -- see the module docstring:
#: a false entity can merge two unrelated events, a missing one only fails to merge.
DEFAULT_MIN_CONFIDENCE = 0.90

#: The model's labels, mapped onto `thedrop_database.enums.EntityType`. MISC covers
#: nationalities, events and works, which this model does not separate; OTHER is honest
#: about that rather than guessing a type the model never predicted.
LABEL_TO_TYPE = {
    "PER": "PERSON",
    "ORG": "ORG",
    "LOC": "PLACE",
    "MISC": "OTHER",
}


def model_name() -> str:
    return os.environ.get("ENTITY_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def min_confidence() -> float:
    raw = os.environ.get("ENTITY_MIN_CONFIDENCE", "").strip()
    try:
        return float(raw) if raw else DEFAULT_MIN_CONFIDENCE
    except ValueError:
        logger.warning("ENTITY_MIN_CONFIDENCE=%r is not a number; using default", raw)
        return DEFAULT_MIN_CONFIDENCE


def is_available() -> bool:
    """Whether this machine can extract, without paying for the import to find out."""
    return importlib.util.find_spec("transformers") is not None


_pipeline = None
_pipeline_lock = threading.Lock()


def _load():
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            import torch
            from transformers import pipeline

            name = model_name()
            device = 0 if torch.cuda.is_available() else -1
            logger.info("loading NER model %s (first use, device=%s)", name, device)
            _pipeline = pipeline(
                "token-classification",
                model=name,
                # Without aggregation the output is word PIECES -- "Pow", "##ell" --
                # which cannot be matched against anything.
                aggregation_strategy="simple",
                device=device,
            )
        return _pipeline


def _clean(surface: str) -> str:
    """Trim the punctuation and articles the tagger sometimes swallows.

    Left deliberately conservative: normalising harder ("Donald Trump" -> "Trump")
    would collapse distinct entities and is entity RESOLUTION, a different problem with
    a different failure mode.
    """
    text = surface.strip().strip(".,;:!?\"'()[]").strip()
    for article in ("the ", "The ", "a ", "A ", "an ", "An "):
        if text.startswith(article):
            text = text[len(article) :]
    return text.strip()


def extract(text: str) -> list[dict[str, object]]:
    """Salient entities in one article's text.

    Returns `[{name, type, mentions, salience}]`, most salient first. `salience` is the
    share of this article's entity mentions that were this entity -- centrality, not
    model confidence. A name mentioned once in passing should not gate a merge, and a
    confident prediction about a passing mention is still a passing mention.
    """
    if not text.strip():
        return []

    predictions = _load()(text)
    threshold = min_confidence()

    counts: Counter[tuple[str, str]] = Counter()
    for prediction in predictions:
        if float(prediction.get("score", 0.0)) < threshold:
            continue
        name = _clean(str(prediction.get("word", "")))
        if len(name) < 2:
            continue
        entity_type = LABEL_TO_TYPE.get(str(prediction.get("entity_group", "")), "OTHER")
        counts[(name, entity_type)] += 1

    total = sum(counts.values())
    if not total:
        return []

    return [
        {
            "name": name,
            "type": entity_type,
            "mentions": mentions,
            "salience": round(mentions / total, 3),
        }
        for (name, entity_type), mentions in counts.most_common()
    ]
