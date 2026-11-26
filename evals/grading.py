"""Deterministic grading of one bot reply against one eval question (no LLM judge).

A question has:
  must_contain     : list of items; each item is a string or {"any_of": [strings]}. ALL items must match.
  must_not_contain : list of strings; NONE may match.
  should_handoff   : bool; the reply's "handoff" must equal it.

Both the reply and the expected strings are normalized the same way first:
  - Arabic-Indic digits -> 0-9, thousands separators removed (1,250 -> 1250)
  - number ranges unified: "1 لـ 2", "1 إلى 2", "1 - 2" -> "1-2"
  - currency: "ج" / "ج.م" / "جنيه" / "جنيهات" / "EGP" / "LE" after a number -> "جنيه"
  - Arabic letter variants (أ إ آ -> ا, ى -> ي, ة -> ه), diacritics and tatweel removed, lowercase
  - punctuation removed; spaces are optional when matching
Latin letters and digits at the edge of an expected string must not touch other Latin letters or digits,
so "L" does not match inside "XL", "XL" not inside "XXL", "7" not inside "17".
"""

import re

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
DIACRITICS = re.compile(r"[ً-ْٰـ]")          # tashkeel + tatweel
THOUSANDS = re.compile(r"(?<=\d)[,٬](?=\d{3}\b)")
RANGE = re.compile(r"(\d+)\s*(?:-|–|—|الي|لحد|ل|او|to)\s*(\d+)")
CURRENCY = re.compile(r"(?<=\d)\s*(?:جنيهات|جنيها|جنيه|ج\s*\.?\s*م|ج|egp|le)(?![a-zء-ي])")
PUNCT = re.compile(r"[^\w\s-]|_")
LATIN = re.compile(r"[a-z0-9]")


def normalize(text):
    t = str(text).translate(ARABIC_DIGITS).lower()
    t = DIACRITICS.sub("", t).translate(LETTERS)
    t = THOUSANDS.sub("", t)
    t = RANGE.sub(r"\1-\2", t)
    t = CURRENCY.sub(" جنيه", t)
    t = PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _pattern(needle):
    n = normalize(needle)
    body = r"\s*".join(re.escape(ch) for ch in n.replace(" ", ""))
    before = r"(?<![a-z0-9])" if LATIN.match(n[:1]) else ""
    after = r"(?![a-z0-9])" if LATIN.match(n[-1:]) else ""
    return re.compile(before + body + after)


def contains(reply, needle):
    return bool(_pattern(needle).search(normalize(reply)))


def _item_ok(reply, item):
    if isinstance(item, dict):
        return any(contains(reply, s) for s in item["any_of"])
    return contains(reply, item)


def grade(question, result):
    """result: {"reply": str, "handoff": bool} -> {"correct": bool, "problems": [str]}"""
    problems = []
    reply = result.get("reply", "")
    if result.get("handoff") != question["should_handoff"]:
        problems.append("false_handoff" if result.get("handoff") else "missed_handoff")
    for item in question.get("must_contain", []):
        if not _item_ok(reply, item):
            label = " | ".join(item["any_of"]) if isinstance(item, dict) else item
            problems.append(f"missing: {label}")
    for bad in question.get("must_not_contain", []):
        if contains(reply, bad):
            problems.append(f"forbidden: {bad}")
    return {"correct": not problems, "problems": problems}
