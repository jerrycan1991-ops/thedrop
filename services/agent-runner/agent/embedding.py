"""The embedding model, on the desktop only.

ADR-0005: one vector space, `bge-small-en-v1.5`, 384 dimensions, normalized, computed
here and posted back. The VPS never loads a model -- torch is 2-4 GB of dependencies
and real CPU contention on a 4-core box that also serves the site.

Two things this module is careful about:

  * **It does not import sentence-transformers at import time.** That import costs
    seconds and drags in torch, which would delay every runner start including ones
    that will never embed anything. The spec is probed instead, and the model is loaded
    on first use.
  * **If the dependency is absent, the handler is not registered.** The runner only
    advertises handlers it has, and the API only leases advertised types -- so a
    desktop without torch simply never receives embedding work, instead of claiming it
    and failing. A warning is logged so a broken install is visible rather than quietly
    idle.

The model is loaded once and reused. Reloading per job would spend several seconds and
a GPU allocation on every batch.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading

logger = logging.getLogger(__name__)

#: Must match the API's `EMBEDDING_MODEL`, which refuses a batch that disagrees. The
#: mismatch is meant to be loud: one vector space is the whole premise of ADR-0005.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIMENSIONS = 384


def model_name() -> str:
    return os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def is_available() -> bool:
    """Whether this machine can embed, without paying for the import to find out."""
    return importlib.util.find_spec("sentence_transformers") is not None


_model = None
_model_lock = threading.Lock()


def _load():
    """Load once. The lock matters because the runner may claim more than one job."""
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            name = model_name()
            logger.info("loading embedding model %s (first use)", name)
            _model = SentenceTransformer(name)
            logger.info("embedding model ready on device %s", _model.device)
        return _model


def encode(texts: list[str]) -> list[list[float]]:
    """Embed a batch, normalized.

    `normalize_embeddings=True` is not optional: the API rejects vectors that are not
    unit length, because a vector off the unit sphere came from a different model or a
    different pooling strategy whatever it claims to be, and cosine similarity over a
    mixed space degrades silently rather than failing.
    """
    model = _load()
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [[float(value) for value in vector] for vector in vectors]
