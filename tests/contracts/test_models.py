import pytest
from pydantic import TypeAdapter, ValidationError

from app.contracts import (
    Access,
    EventCard,
    Evidence,
    Feature,
    Insight,
    InsightResult,
    NewsRef,
    SessionCtx,
    UserRole,
)

EVIDENCE = {
    "id": "E1",
    "type": "news",
    "text": "Совет директоров Банка России сохранил ключевую ставку на уровне 14%.",
    "ref": {
        "kind": "news",
        "event_id": 1,
        "raw_item_id": 10,
        "url": "https://example.test/a",
        "source_name": "Банк России",
        "published_at": "2026-09-11T13:30:00+03:00",
    },
}


def statement(evidence: list[str]) -> dict:
    return {
        "id": "s1",
        "text": "Ставка сохранена.",
        "origin": "fact",
        "evidence": evidence,
        "confidence": "high",
    }


def insight_dict(**over) -> dict:
    base = {
        "headline": "Деньги остаются дорогими",
        "impact": {"direction": "risk", "strength": "high", "horizon": "months"},
        "what_happened": [statement(["E1"])],
        "assumptions": [],
        "why_it_matters": [],
        "risks": [],
        "opportunities": [],
        "watch": [],
        "scenarios": [],
        "actions": [],
        "questions": [],
        "bank_view": {"statements": [], "as_of": None, "match": "no_forecast", "gap": "Позиции банка нет."},
        "bank_offer": None,
        "confidence": "medium",
        "limitations": [],
    }
    return base | over


def card_dict() -> dict:
    return {
        "summary": "ЦБ сохранил ставку.",
        "facts": [],
        "claims": [],
        "actors": [],
        "dates": [],
        "numbers": [],
        "causes": [],
        "event_type": "regulator_decision",
        "factors": [],
        "sectors": [],
        "regions": [],
        "horizon": "months",
        "certainty": "decided",
    }


def result_dict(insight: dict, evidence: list[dict] | None = None) -> dict:
    return {
        "insight": insight,
        "evidence": [EVIDENCE] if evidence is None else evidence,
        "mode": "full",
        "checks": {"statements_total": 1, "passed_first_try": 1, "removed": 0, "revisions": 0},
    }


def test_extra_field_rejected_on_card():
    with pytest.raises(ValidationError):
        EventCard.model_validate(card_dict() | {"lead": "лишнее"})


def test_extra_field_rejected_on_insight():
    with pytest.raises(ValidationError):
        Insight.model_validate(insight_dict(lead="лишнее"))


def test_valid_minimal_card_and_insight():
    EventCard.model_validate(card_dict())
    Insight.model_validate(insight_dict())


def test_headline_limit():
    Insight.model_validate(insight_dict(headline="а" * 110))
    with pytest.raises(ValidationError):
        Insight.model_validate(insight_dict(headline="а" * 111))


def test_evidence_type_must_match_ref_kind():
    ref = NewsRef.model_validate(EVIDENCE["ref"])
    with pytest.raises(ValidationError, match="не подходит"):
        Evidence(id="E1", type="bank", text="x", ref=ref)


def test_insight_result_rejects_unknown_evidence_id():
    with pytest.raises(ValidationError, match="E9"):
        InsightResult.model_validate(result_dict(insight_dict(what_happened=[statement(["E9"])])))


def test_insight_result_rejects_unknown_scenario_evidence():
    scenario = {"condition": "если инфляция вырастет", "outcome": "ставку не снизят", "evidence": ["E7"]}
    with pytest.raises(ValidationError, match="E7"):
        InsightResult.model_validate(result_dict(insight_dict(scenarios=[scenario])))


def test_insight_result_accepts_known_evidence():
    InsightResult.model_validate(result_dict(insight_dict()))


def test_insight_result_rejects_duplicate_evidence_ids():
    with pytest.raises(ValidationError, match="повторяются"):
        InsightResult.model_validate(result_dict(insight_dict(), [EVIDENCE, EVIDENCE]))


def test_session_ctx_is_frozen():
    ctx = SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.client)
    with pytest.raises(ValidationError):
        ctx.company_id = "C-IT-01"


def test_feature_source_pattern():
    Feature(key="products.loa", value=True, source="csv:REVENUE_COMPANY", detail=None)
    Feature(key="suppliers.metallurgy", value=0.31, source="calc:suppliers", detail=None)
    Feature(key="sector", value="development", source="group", detail=None)
    with pytest.raises(ValidationError):
        Feature(key="x", value=1, source="excel:x", detail=None)


@pytest.mark.parametrize("value", [14, "строка", [1, "a", None], {"a": {"b": [1]}}, None, True])
def test_feature_value_accepts_json(value):
    feature = Feature(key="k", value=value, source="user", detail=None)
    assert TypeAdapter(Feature).validate_json(feature.model_dump_json()).value == value
