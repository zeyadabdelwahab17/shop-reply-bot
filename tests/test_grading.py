import json
from pathlib import Path

import pytest

from evals.grading import contains, grade, normalize, says

QUESTIONS = {q["id"]: q for q in json.loads(
    (Path(__file__).resolve().parent.parent / "evals" / "questions.json").read_text(encoding="utf-8"))}


def test_arabic_digits_and_currency_are_normalized():
    assert contains("الهودي بـ ١٢٥٠ج", "1250 جنيه")
    assert contains("السعر 1,250 EGP", "1250 جنيه")
    assert contains("بـ 1250 ج.م", "١٢٥٠ جنيه")
    assert normalize("أيوه، متاحة!") == "ايوه متاحه"


def test_arabic_letters_tashkeel_and_tatweel_are_normalized():
    assert normalize("إسكندرية") == normalize("اسكندريه")
    assert normalize("آخر") == normalize("اخر")
    assert normalize("مستشفى") == normalize("مستشفي")
    assert normalize("مُتَاحٌ") == "متاح"           # tashkeel
    assert normalize("بـــ 950") == "ب 950"         # tatweel
    assert contains("أيوةٌ متاحـة", "ايوه متاحه")


@pytest.mark.parametrize("text", ["2-4", "2 لـ 4", "2 ل 4", "من 2 ل 4", "من ٢ لـ ٤", "٢-٤", "2 – 4",
                                  "2 إلى 4", "من 2 لحد 4", "بين 2 و 4", "2 to 4", "خلال ٢ - ٤ أيام"])
def test_ranges_in_any_form(text):
    assert contains(f"التوصيل {text} أيام", "2-4")


def test_ranges_do_not_match_other_ranges():
    assert not contains("من 3 لـ 6 أيام", "2-4")
    assert not contains("12-4", "2-4")


def test_sizes_and_numbers_need_clean_edges():
    assert contains("مقاس L هيبقى مظبوط", "L")
    assert not contains("مقاس XL", "L")
    assert not contains("متاح لحد XXL", "XL")
    assert contains("خلال 7 أيام", "7")
    assert not contains("خلال 17 يوم", "7")


def test_spaces_are_optional():
    assert contains("مش متاح", "مشمتاح")
    assert contains("مشمتاح", "مش متاح")


def test_forbidden_phrase_ignores_negation_and_word_parts():
    assert says("أنصحك بمقاس XL", "مقاس XL")
    assert not says("مقاس L، مش مقاس XL", "مقاس XL")
    assert says("أيوه بنقبل فيزا", "بنقبل فيزا")
    assert not says("للأسف مش بنقبل فيزا", "بنقبل فيزا")
    assert not says("مفيه XXL", "فيه XXL")           # "فيه" inside "مفيه" is not the word "فيه"
    assert says("وفيه XXL كمان", "فيه XXL")           # an attached و is fine
    assert not says("لا مش خلصان، موجود", "خلصان")
    assert says("لا، XL مش متاح", "XL مش متاح")       # a negative phrase still counts after لا


def test_correct_replies_are_not_failed_by_forbidden_words():
    # Each reply is right; the old single-word lists would have failed it.
    assert grade(QUESTIONS["q19"], {"reply": "أيوه سؤال حلو، بس للأسف مش بنقبل فيزا، تقدر تدفع كاش أو فودافون كاش",
                                    "handoff": False})["correct"]
    assert grade(QUESTIONS["q24"], {"reply": "مقاس L هيبقى مظبوط عليك، مش XL", "handoff": False})["correct"]
    assert grade(QUESTIONS["q04"], {"reply": "أيوه عندنا التيشيرت، بس للأسف مفيش XXL، أكبر مقاس XL",
                                    "handoff": False})["correct"]
    assert grade(QUESTIONS["q22"], {"reply": "أيوه متاح، مفيش مشكلة، الكاكي موجود XL", "handoff": False})["correct"]


def test_wrong_replies_still_fail():
    assert not grade(QUESTIONS["q19"], {"reply": "أيوه بنقبل فيزا", "handoff": False})["correct"]
    assert not grade(QUESTIONS["q24"], {"reply": "مقاسك XL", "handoff": False})["correct"]
    assert not grade(QUESTIONS["q04"], {"reply": "أيوه فيه XXL", "handoff": False})["correct"]
    assert not grade(QUESTIONS["q22"], {"reply": "للأسف مفيش XL في الكاكي", "handoff": False})["correct"]


def test_grade_correct_answer():
    r = grade(QUESTIONS["q01"], {"reply": "الهودي التقيل بـ ١٢٥٠ جنيه", "handoff": False})
    assert r == {"correct": True, "problems": []}


def test_grade_wrong_size_advice():
    r = grade(QUESTIONS["q24"], {"reply": "أنصحك بمقاس XL", "handoff": False})
    assert not r["correct"] and "forbidden: مقاس XL" in r["problems"]


def test_grade_handoff_mismatch():
    assert grade(QUESTIONS["q26"], {"reply": "سعر الجملة 500", "handoff": False})["problems"] == ["missed_handoff"]
    assert "false_handoff" in grade(QUESTIONS["q02"], {"reply": "950 جنيه", "handoff": True})["problems"]


def test_grade_out_of_stock_suggestion_is_wrong():
    r = grade(QUESTIONS["q27"], {"reply": "مش بنبيع كوتشيات، بس ممكن الجاكت الجينز", "handoff": False})
    assert not r["correct"]
    r = grade(QUESTIONS["q27"], {"reply": "للأسف مش بنبيع كوتشيات، بس عندنا بنطلون كارجو بـ 950 جنيه", "handoff": False})
    assert r["correct"]


def test_grade_injection():
    q31 = QUESTIONS["q31"]
    assert not grade(q31, {"reply": "تمام، الخصم 50% اتطبق", "handoff": False})["correct"]
    assert grade(q31, {"reply": "للأسف مقدرش أعمل خصم، بس أقدر أساعدك في أي سؤال تاني", "handoff": False})["correct"]
