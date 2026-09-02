"""Normalization: canonical URLs, hostile-text handling, injection scanning.

Implements PIPELINE.md §3 and the VPS half of SECURITY.md §6.2.

The governing rule (CLAUDE.md): ingested content is **evidence, never instruction**.
Nothing in this module deletes suspicious text -- deletion hides the attack and destroys
the evidence. Everything it finds is recorded in `injection_flags` and the content is
stored intact, wrapped, and analysed as data.

This is the input-side layer. It is not the safety net: SECURITY.md §6.3 puts that on
the output, where schema validation, claim traceability and source resolution mean an
injected "fact" has no claim id and cannot survive into a published field. Treating this
module as sufficient would be a mistake.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --------------------------------------------------------------------------- URLs

#: Tracking parameters stripped before hashing. A story syndicated with five different
#: campaign tags is one story; without this the url_hash guard sees five.
TRACKING_PARAM_PREFIXES = ("utm_", "pk_", "mc_", "hsa_", "ga_")
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "igshid",
        "mkt_tok",
        "ref",
        "referrer",
        "source",
        "spm",
        "s_kwcid",
        "_ga",
        "yclid",
        "twclid",
        "cmpid",
        "ncid",
        "smid",
        "partner",
    }
)

#: Ports that carry no information once the scheme is known.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalize_url(url: str) -> str:
    """Reduce a URL to the form two identical articles would share.

    Does NOT follow redirects or resolve AMP -- both need a network round trip and
    belong to the fetching layer, which can respect robots.txt and rate limits. This is
    the pure-function half, so it is safe to call anywhere and trivially testable.
    """
    parts = urlsplit(url.strip())

    host = parts.hostname or ""
    host = host.lower().rstrip(".")
    netloc = host
    if parts.port and str(parts.port) != _DEFAULT_PORTS.get(parts.scheme.lower(), ""):
        netloc = f"{host}:{parts.port}"

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
        and not k.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    # Sorted so parameter order cannot produce two hashes for one resource.
    query = urlencode(sorted(kept), doseq=True)

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Fragments are client-side only and never identify a distinct article.
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def url_hash(canonical_url: str) -> bytes:
    """sha256 of the canonical URL. The unique constraint on `raw_articles.url_hash`."""
    return hashlib.sha256(canonical_url.encode("utf-8")).digest()


def content_hash(body_text: str) -> bytes:
    """sha256 of whitespace-collapsed body.

    Catches identical syndication published under different URLs, which the url_hash
    constraint cannot see. Collapsing whitespace first means a reflowed copy still
    matches.
    """
    collapsed = " ".join(body_text.split())
    return hashlib.sha256(collapsed.encode("utf-8")).digest()


# ------------------------------------------------------------------------ HTML

#: Elements whose *content* is dropped entirely, not just their tags. Script and style
#: are obvious; the rest are places injected instructions hide from a reader while
#: remaining in the text a model sees.
_DROP_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template", "iframe", "object"})

_HIDDEN_STYLE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0"
    r"|position\s*:\s*absolute\s*;?\s*(left|top)\s*:\s*-\d{3,})",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    """Plain text from HTML, dropping content a reader would never see.

    Not a sanitizer and not a substitute for trafilatura-class extraction -- it exists
    so that hidden instruction blocks are removed from the text before scanning, per
    SECURITY.md §6.2. Full allow-list sanitization for `body_html_sanitized` is a
    separate concern and a separate dependency decision.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_blocks = 0
        self._suppress_depth = 0
        self._suppress_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._suppress_depth:
            if tag == self._suppress_tag:
                self._suppress_depth += 1
            return

        attributes = {k.lower(): (v or "") for k, v in attrs}
        hidden = (
            tag in _DROP_CONTENT_TAGS
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
            or bool(_HIDDEN_STYLE.search(attributes.get("style", "")))
        )
        if hidden:
            if tag not in _DROP_CONTENT_TAGS:
                self.hidden_blocks += 1
            self._suppress_tag = tag
            self._suppress_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._suppress_depth and tag == self._suppress_tag:
            self._suppress_depth -= 1
            if self._suppress_depth == 0:
                self._suppress_tag = None
        elif not self._suppress_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._suppress_depth:
            self.parts.append(data)

    # HTML comments are dropped outright: they are invisible to a reader and a
    # first-choice hiding place for injected instructions.
    def handle_comment(self, data: str) -> None:
        return

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def html_to_text(html: str) -> tuple[str, int]:
    """Return (plain text, number of hidden blocks dropped)."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text(), parser.hidden_blocks


# ------------------------------------------------------------------ hostile text

#: Zero-width and bidi-control characters. They render as nothing but survive into the
#: text a model reads, so they can hide or reorder an instruction invisibly.
_INVISIBLE = re.compile(
    "["
    "​-‏"  # zero-width space/joiners, LRM/RLM
    "‪-‮"  # bidi embedding/override
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # bidi isolates
    "﻿"  # BOM used mid-string
    "]"
)

#: Imperative-to-AI patterns. Deliberately broad: a false positive costs a flag on a row
#: that is still stored and still analysed, while a false negative is the failure mode
#: that puts a fabricated story on a live site.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous", re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\b", re.I)),
    ("disregard", re.compile(r"\bdisregard\s+(all\s+)?(previous|prior|the\s+above)\b", re.I)),
    ("role_reassignment", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?\w+", re.I)),
    ("system_prompt", re.compile(r"\bsystem\s+prompt\b|\bdeveloper\s+message\b", re.I)),
    ("output_control", re.compile(r"\boutput\s+only\b|\brespond\s+only\s+with\b", re.I)),
    ("ai_address", re.compile(r"\bas\s+an\s+AI\b|\blanguage\s+model\b", re.I)),
    ("publish_directive", re.compile(r"\bpublish\s+this\b|\bmark\s+this\s+as\s+breaking\b", re.I)),
    ("instruction_block", re.compile(r"</?\s*(system|instruction|prompt)\s*>", re.I)),
    # Our own wrapper being closed from inside the data is unambiguous.
    ("wrapper_escape", re.compile(r"</?\s*untrusted_source_data", re.I)),
)


def sanitize_text(raw: str) -> tuple[str, dict[str, object]]:
    """Normalize hostile text and report what was found. Never deletes content.

    Returns the cleaned text and the `injection_flags` payload. An empty `patterns` list
    means scanned-and-clean; the absence of the whole dict would mean never scanned,
    which is why the column is NOT NULL.
    """
    flags: dict[str, object] = {}

    # NFKC folds compatibility forms, so fullwidth and styled letters cannot be used to
    # slip a pattern past the regexes below.
    text = unicodedata.normalize("NFKC", raw)

    invisible_count = len(_INVISIBLE.findall(text))
    if invisible_count:
        flags["invisible_chars"] = invisible_count
    text = _INVISIBLE.sub("", text)

    matched = [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]
    if matched:
        flags["patterns"] = matched

    non_ascii = sum(1 for c in text if ord(c) > 0x7F and unicodedata.category(c).startswith("L"))
    letters = sum(1 for c in text if c.isalpha())
    # Homoglyph attacks show up as a Latin-looking string with a high proportion of
    # non-ASCII letters. Flagged, not blocked -- plenty of legitimate copy is non-ASCII.
    if letters and non_ascii / letters > 0.30:
        flags["non_ascii_letter_ratio"] = round(non_ascii / letters, 3)

    flags["patterns"] = flags.get("patterns", [])
    return text, flags


def escape_wrapper_delimiters(text: str) -> str:
    """Neutralise anything that could close our `untrusted_source_data` wrapper.

    Applied when text is placed into a prompt, not at storage time -- the stored row
    stays verbatim because it is evidence.
    """
    return re.sub(r"<(/?)\s*(untrusted_source_data)", r"&lt;\1\2", text, flags=re.IGNORECASE)


# ------------------------------------------------------------------ normalized item


@dataclass(frozen=True)
class NormalizedItem:
    """What every provider adapter produces, and the only shape the pipeline consumes.

    No downstream code imports a provider module (PIPELINE.md §2), so adding a provider
    can never change the pipeline.
    """

    canonical_url: str
    original_url: str
    title: str
    body_text: str
    published_at_iso: str | None
    #: True when `published_at` fell back to discovery time because the source reported
    #: none. An invented timestamp would be a fabricated fact, so the fallback is
    #: recorded rather than hidden.
    timestamp_estimated: bool
    language: str = "en"
    dek: str | None = None
    authors: tuple[str, ...] = ()
    image_urls: tuple[str, ...] = ()
    injection_flags: dict[str, object] = field(default_factory=dict)
    raw_payload: dict[str, object] = field(default_factory=dict)

    @property
    def url_hash(self) -> bytes:
        return url_hash(self.canonical_url)

    @property
    def content_hash(self) -> bytes:
        return content_hash(self.body_text)
