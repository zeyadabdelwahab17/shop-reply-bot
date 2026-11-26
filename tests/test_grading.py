import json
from pathlib import Path

from evals.grading import contains, grade, normalize

QUESTIONS = {q["id"]: q for q in json.loads(
    (Path(__file__).resolve().parent.parent / "evals" / "questions.json").read_text(encoding="utf-8"))}


def test_arabic_digits_and_currency_are_normalized():
    assert contains("الهودي بـ ١٢٥٠ج", "1250 جنيه")
    assert contains("السعر 1,250 EGP", "1250 جنيه")
    assert contains("بـ 1250 ج.م", "١٢٥٠ جنيه")
    assert normalize("أيوه، متاحة!") == "ايوه متاحه"


def test_ranges_are_normalized():
    assert contains("التوصيل من 1 لـ 2 يوم", "1-2")
    assert contains("هيوصلك خلال ٢ - ٤ أيام", "2-4")
    assert contains("من 3 إلى 6 أيام", "3-6")


def test_sizes_and_numbers_need_clean_edges():
    assert contains("مقاس L هيبقى مظبوط", "L")
    assert not contains("مقاس XL", "L")
    assert not contains("متاح لحد XXL", "XL")
    assert contains("خلال 7 أيام", "7")
    assert not contains("خلال 17 يوم", "7")


def test_spaces_are_optional():
    assert contains("مش متاح", "مشمتاح")
    assert contains("مشمتاح", "مش متاح")


def test_grade_correct_answer():
    r = grade(QUESTIONS["q01"], {"reply": "الهودي التقيل بـ ١٢٥٠ جنيه", "handoff": False})
    assert r == {"correct": True, "problems": []}


def test_grade_wrong_size_advice():
    r = grade(QUESTIONS["q24"], {"reply": "أنصحك بمقاس XL", "handoff": False})
    assert not r["correct"] and "forbidden: XL" in r["problems"]


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
