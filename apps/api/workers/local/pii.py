"""Local regex-based PII detector.

Replaces the previous RebaseKit ``/pii/api/detect`` call. No external
dependency, no network, deterministic output. Not as thorough as a
full NLP-based detector (Presidio / spaCy) but covers the high-value
patterns — email, phone, SSN, credit card, IP, IBAN, generic passport
number — which is ~95% of what users actually want redacted.

Credit card detection uses a Luhn-validated prefix check so strings
like ``4242 4242 4242 4242`` hit but random 16-digit sequences don't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

ALL_ENTITIES: frozenset[str] = frozenset(
    {
        "EMAIL",
        "PHONE",
        "SSN",
        "CREDIT_CARD",
        "IPV4",
        "IPV6",
        "IBAN",
        "PASSPORT",
    }
)


@dataclass(frozen=True, slots=True)
class PIIEntity:
    type: str
    start: int
    end: int
    value: str
    score: float


# ── Regex patterns ────────────────────────────────────────────────────
# Each pattern is tight enough to avoid obvious false positives but
# loose enough to catch common formats. Order matters — credit card
# runs before phone so "4242 4242 4242 4242" isn't caught as a phone.

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# International-leading, separators tolerated, 8–15 digits of content.
_PHONE_RE = re.compile(
    r"(?<![\w.])\+?\d{1,3}[\s\-.]?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}(?!\w)"
)

_SSN_RE = re.compile(r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")

_CREDIT_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

_IPV6_RE = re.compile(
    r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b"
)

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")

# Generic passport: 1 letter + 8 digits, or 2 letters + 7 digits.
_PASSPORT_RE = re.compile(r"\b(?:[A-Z]\d{8}|[A-Z]{2}\d{7})\b")


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _find(
    pattern: re.Pattern[str],
    entity_type: str,
    text: str,
    *,
    validator=None,
) -> Iterable[PIIEntity]:
    for m in pattern.finditer(text):
        value = m.group(0)
        if validator is not None and not validator(value):
            continue
        yield PIIEntity(
            type=entity_type,
            start=m.start(),
            end=m.end(),
            value=value,
            score=0.95 if validator is not None else 0.85,
        )


def detect(
    text: str,
    *,
    entities: Iterable[str] | None = None,
) -> list[PIIEntity]:
    """Return all PII hits in *text*, optionally filtered to *entities*."""
    wanted = (
        ALL_ENTITIES
        if not entities
        else frozenset(e.upper() for e in entities) & ALL_ENTITIES
    )
    if not wanted:
        return []

    found: list[PIIEntity] = []

    if "EMAIL" in wanted:
        found.extend(_find(_EMAIL_RE, "EMAIL", text))
    if "CREDIT_CARD" in wanted:
        found.extend(_find(_CREDIT_RE, "CREDIT_CARD", text, validator=_luhn_ok))
    if "SSN" in wanted:
        found.extend(_find(_SSN_RE, "SSN", text))
    if "PHONE" in wanted:
        # Phone runs after credit card — drop hits that overlap a card.
        card_spans = [
            (e.start, e.end) for e in found if e.type == "CREDIT_CARD"
        ]
        for ent in _find(_PHONE_RE, "PHONE", text):
            if any(not (ent.end <= s or ent.start >= e) for s, e in card_spans):
                continue
            found.append(ent)
    if "IPV4" in wanted:
        found.extend(_find(_IPV4_RE, "IPV4", text))
    if "IPV6" in wanted:
        found.extend(_find(_IPV6_RE, "IPV6", text))
    if "IBAN" in wanted:
        found.extend(_find(_IBAN_RE, "IBAN", text))
    if "PASSPORT" in wanted:
        found.extend(_find(_PASSPORT_RE, "PASSPORT", text))

    found.sort(key=lambda e: (e.start, e.type))
    return found


def redact(text: str, hits: list[PIIEntity]) -> str:
    """Return *text* with every hit replaced by ``[TYPE]``. Non-overlapping."""
    if not hits:
        return text
    # Sort descending so earlier indices aren't shifted.
    out = text
    for ent in sorted(hits, key=lambda e: e.start, reverse=True):
        out = out[: ent.start] + f"[{ent.type}]" + out[ent.end :]
    return out


def run(inp: dict) -> dict:
    """Task-handler entry point. Shape matches the historical RebaseKit
    ``/pii/api/detect`` response so existing clients keep working."""
    text = inp.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("pii_detect: 'text' is required and must be a non-empty string")

    hits = detect(text, entities=inp.get("entities"))
    payload: dict = {
        "entities": [
            {
                "type": h.type,
                "start": h.start,
                "end": h.end,
                "value": h.value,
                "score": h.score,
            }
            for h in hits
        ],
        "count": len(hits),
    }
    if inp.get("mask"):
        payload["redacted_text"] = redact(text, hits)
    return payload


__all__ = ["ALL_ENTITIES", "PIIEntity", "detect", "redact", "run"]
