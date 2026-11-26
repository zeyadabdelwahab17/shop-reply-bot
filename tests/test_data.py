import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOP_FILE = ROOT / "data" / "shop_info.json"
QUESTIONS_FILE = ROOT / "evals" / "questions.json"

QUESTION_FIELDS = {"id", "question", "expected_answer", "must_contain", "must_not_contain", "category", "should_handoff"}
PRODUCT_FIELDS = {"name", "price_egp", "sizes", "colors", "in_stock"}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_shop_info_loads():
    shop = load(SHOP_FILE)
    for key in ("products", "size_guide", "shipping", "returns", "payment_methods", "working_hours"):
        assert key in shop, f"shop_info.json is missing '{key}'"
    assert 10 <= len(shop["products"]) <= 12
    for p in shop["products"]:
        assert PRODUCT_FIELDS <= p.keys(), f"product {p.get('name')} is missing fields"


def test_handoff_message_is_honest():
    # No promise of an instant reply: a person answers during working hours.
    assert load(SHOP_FILE)["handoff_message"] == "هحوّلك لحد من المحل وهيرد عليك في أقرب وقت خلال مواعيد العمل"


def test_questions_load_and_have_all_fields():
    questions = load(QUESTIONS_FILE)
    assert len(questions) == 31
    for q in questions:
        missing = QUESTION_FIELDS - q.keys()
        assert not missing, f"{q.get('id')} is missing {missing}"
        assert q["question"].strip()
        assert isinstance(q["expected_answer"], list) and q["expected_answer"]
        assert isinstance(q["should_handoff"], bool)
        for item in q["must_contain"]:
            if isinstance(item, dict):
                assert set(item) == {"any_of"} and item["any_of"], f"{q['id']}: bad any_of group"
                assert all(isinstance(s, str) and s.strip() for s in item["any_of"])
            else:
                assert isinstance(item, str) and item.strip(), f"{q['id']}: bad must_contain item"
        assert all(isinstance(s, str) and s.strip() for s in q["must_not_contain"])
        # Questions that are answered must check at least one fact.
        if not q["should_handoff"]:
            assert q["must_contain"], f"{q['id']} has nothing to check"


def test_question_ids_unique_and_mix_correct():
    questions = load(QUESTIONS_FILE)
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "duplicate question ids"
    assert sum(q["should_handoff"] for q in questions) == 5
    # Products the shop doesn't sell are answered (not available + suggestion), not handed off.
    not_sold = [q for q in questions if q["category"] == "not_in_catalog"]
    assert not_sold and not any(q["should_handoff"] for q in not_sold)
    assert sum(q["category"].startswith("tricky_") for q in questions) == 4
    # Prompt injection asking for a discount: refuse politely, no handoff.
    injection = [q for q in questions if q["category"] == "injection"]
    assert [q["id"] for q in injection] == ["q31"] and not injection[0]["should_handoff"]


def test_q01_accepts_either_hoodie_price():
    q01 = next(q for q in load(QUESTIONS_FILE) if q["id"] == "q01")
    assert q01["must_contain"] == [{"any_of": ["1250", "1400"]}]
