"""Deterministic grading of one bot reply against one eval question (no LLM judge).

A question has:
  must_contain     : list of items; each item is a string or {"any_of": [strings]}. ALL items must match.
  must_not_contain : list of strings; NONE may match.
  should_handoff   : bool; the reply's "handoff" must equal it.

Both the reply and the expected strings are normalized the same way first:
  - Arabic-Indic digits -> 0-9, thousands separators removed (1,250 -> 1250)
  - number ranges unified: "2 لـ 4", "من 2 ل 4", "2 إلى 4", "بين 2 و 4", "٢-٤", "2 – 4" -> "2-4"
  - currency: "ج" / "ج.م" / "جنيه" / "جنيهات" / "EGP" / "LE" after a number -> "جنيه"
  - Arabic letter variants (أ إ آ -> ا, ى -> ي, ة -> ه), diacritics and tatweel removed, lowercase
  - punctuation removed; spaces are optional when matching
Latin letters and digits at the edge of an expected string must not touch other Latin letters or digits,
so "L" does not match inside "XL", "XL" not inside "XXL", "7" not inside "17".

must_not_contain entries are phrases (e.g. "أيوه بنقبل فيزا", "مقاس XL"), matched more strictly:
  - the phrase must start at a word (an attached و / ف / ب / ل / ال is fine), so "فيه XXL" does not
    match inside "مفيه XXL";
  - a phrase that has no negation in it does not count when one of the two words
    before it is a negation (مش, لا, مفيش, مو, غير, ...): "مش مقاس XL" or "مش بنقبل فيزا" are fine.
"""

import re

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
DIACRITICS = re.compile(r"[ً-ْٰـ]")          # tashkeel + tatweel
THOUSANDS = re.compile(r"(?<=\d)[,٬](?=\d{3}\b)")
RANGE = re.compile(r"(\d+)\s*(?:-|–|—|الي|لحد|لغايه|ل|او|to)\s*(\d+)")
BETWEEN = re.compile(r"بين\s*(\d+)\s*و\s*(\d+)")
NEGATIONS = {"مش", "مو", "لا", "مفيش", "مافيش", "ما", "غير", "بلاش", "مبقاش",
             "مقدرش", "مبقدرش", "مينفعش", "معنديش", "معندناش", "ملناش"}
PROCLITICS = {"", "و", "ف", "ب", "ل", "ك", "ال", "وال", "بال", "فال", "لل", "وب", "ول"}
CURRENCY = re.compile(r"(?<=\d)\s*(?:جنيهات|جنيها|جنيه|ج\s*\.?\s*م|ج|egp|le)(?![a-zء-ي])")
PUNCT = re.compile(r"[^\w\s-]|_")
LATIN = re.compile(r"[a-z0-9]")


def normalize(text):
    t = str(text).translate(ARABIC_DIGITS).lower()
    t = DIACRITICS.sub("", t).translate(LETTERS)
    t = THOUSANDS.sub("", t)
    t = BETWEEN.sub(r"\1-\2", t)
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


def says(reply, phrase):
    """Strict match for must_not_contain: word start, and not negated right before (see module doc)."""
    text = normalize(reply)
    phrase_negative = any(w in NEGATIONS for w in normalize(phrase).split(" "))   # e.g. "XL مش متاح"
    for m in _pattern(phrase).finditer(text):
        before = text[: m.start()]
        attached = before.rsplit(" ", 1)[-1]          # letters glued to the front of the match
        if attached not in PROCLITICS:
            continue
        words_before = before[: len(before) - len(attached)].split()[-2:]
        if not phrase_negative and any(w in NEGATIONS for w in words_before):
            continue
        return True
    return False


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
        if says(reply, bad):
            problems.append(f"forbidden: {bad}")
    return {"correct": not problems, "problems": problems}
