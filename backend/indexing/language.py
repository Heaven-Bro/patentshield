"""Lightweight Unicode-script language detection and source-language tokenization.

This intentionally detects only script evidence relevant to PatentShield's CJK
cross-lingual workflow. It is not a statistical language identifier: Han-only
text is marked Chinese with medium confidence because Japanese text can contain
Han characters without kana. Non-CJK text remains ``und`` rather than being
incorrectly labelled English.
"""

from __future__ import annotations

import re
from typing import TypedDict

_WORD_RE = re.compile(r"[\w]+", flags=re.UNICODE)


class LanguageDetection(TypedDict):
    language: str
    confidence: str
    method: str


def _is_han(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _is_hiragana_or_katakana(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3040 <= codepoint <= 0x309F
        or 0x30A0 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0xFF66 <= codepoint <= 0xFF9D
    )


def _is_hangul(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xD7B0 <= codepoint <= 0xD7FF
    )


def is_cjk_character(char: str) -> bool:
    """Return whether a code point belongs to a Chinese/Japanese/Korean script."""
    return _is_han(char) or _is_hiragana_or_katakana(char) or _is_hangul(char)


def detect_language(text: str) -> LanguageDetection:
    """Detect Chinese, Japanese, or Korean using Unicode script signals.

    ``method`` is returned with each result so clients do not mistake this
    heuristic for a statistical, document-level language classifier.
    """
    if not isinstance(text, str) or not text.strip():
        return {"language": "und", "confidence": "low", "method": "unicode_script"}

    han_count = 0
    kana_count = 0
    hangul_count = 0
    cjk_count = 0
    for char in text:
        if _is_han(char):
            han_count += 1
            cjk_count += 1
        elif _is_hiragana_or_katakana(char):
            kana_count += 1
            cjk_count += 1
        elif _is_hangul(char):
            hangul_count += 1
            cjk_count += 1

    if cjk_count == 0:
        return {"language": "und", "confidence": "low", "method": "unicode_script"}
    if hangul_count:
        return {"language": "ko", "confidence": "high", "method": "unicode_script"}
    if kana_count:
        return {"language": "ja", "confidence": "high", "method": "unicode_script"}
    if han_count:
        confidence = "medium" if han_count >= 2 else "low"
        return {"language": "zh", "confidence": confidence, "method": "unicode_script"}
    return {"language": "und", "confidence": "low", "method": "unicode_script"}


def detect_language_code(text: str) -> str:
    """Return the compact language code used in indexed text-chunk metadata."""
    return detect_language(text)["language"]


def has_substantial_cjk(text: str, *, min_characters: int = 8, min_ratio: float = 0.15) -> bool:
    """Whether CJK script dominates enough text to use character-based chunking.

    A few CJK identifiers or foreign-language names inside a mostly Latin patent
    should not force the complete English specification into character windows.
    """
    visible_characters = sum(1 for char in text if not char.isspace())
    if visible_characters == 0:
        return False
    cjk_characters = sum(1 for char in text if is_cjk_character(char))
    return (
        cjk_characters >= min_characters
        and cjk_characters / visible_characters >= min_ratio
    )


def _split_cjk_runs(token: str) -> list[tuple[str, bool]]:
    """Split a Unicode word into contiguous CJK and non-CJK segments."""
    output: list[tuple[str, bool]] = []
    current: list[str] = []
    current_is_cjk: bool | None = None
    for char in token:
        char_is_cjk = is_cjk_character(char)
        if current and char_is_cjk != current_is_cjk:
            output.append(("".join(current), bool(current_is_cjk)))
            current = []
        current.append(char)
        current_is_cjk = char_is_cjk
    if current:
        output.append(("".join(current), bool(current_is_cjk)))
    return output


def tokenize_original_language(text: str) -> list[str]:
    """Tokenize Latin terms and CJK character n-grams without translating.

    CJK runs emit character unigrams, bigrams, and trigrams. This makes lexical
    retrieval work when Chinese/Japanese/Korean text has no whitespace between
    words, while retaining patent identifiers and other non-CJK terms as whole
    tokens. The original source text is always the indexed/result text.
    """
    tokens: list[str] = []
    for word in _WORD_RE.findall(text or ""):
        for segment, is_cjk in _split_cjk_runs(word):
            segment = segment.casefold()
            if not is_cjk:
                if segment:
                    tokens.append(segment)
                continue

            chars = list(segment)
            if segment:
                # The whole run improves exact short-code matches; for long runs
                # n-grams avoid making a complete sentence a single BM25 token.
                if len(chars) <= 4:
                    tokens.append(segment)
                tokens.extend(chars)
                tokens.extend(segment[index:index + 2] for index in range(len(segment) - 1))
                tokens.extend(segment[index:index + 3] for index in range(len(segment) - 2))
    return tokens
