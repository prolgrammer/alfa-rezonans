import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import contracts
from app.clients.profile import profile_compact
from app.contracts import (
    Access,
    AgentStep,
    CompanyProfile,
    Event,
    EventCard,
    Evidence,
    Feature,
    Hypothesis,
    Insight,
    InsightContext,
    InsightResult,
    NewsRef,
    SessionCtx,
    Spent,
    TraceRecord,
    UserRole,
)
from app.fixtures import load_fixture

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
    Feature(key="products.LOA", value=True, source="csv:REVENUE_COMPANY", detail=None)
    Feature(key="suppliers.metallurgy", value=0.31, source="calc:suppliers", detail=None)
    Feature(key="sector", value="development", source="group", detail=None)
    with pytest.raises(ValidationError):
        Feature(key="x", value=1, source="excel:x", detail=None)


@pytest.mark.parametrize("value", [14, "строка", [1, "a", None], {"a": {"b": [1]}}, None, True])
def test_feature_value_accepts_json(value):
    feature = Feature(key="k", value=value, source="user", detail=None)
    assert TypeAdapter(Feature).validate_json(feature.model_dump_json()).value == value


def test_naive_datetime_rejected():
    ref = dict(EVIDENCE["ref"])
    NewsRef.model_validate(ref)
    ref["published_at"] = "2026-09-11T13:30:00"
    with pytest.raises(ValidationError):
        NewsRef.model_validate(ref)


def test_every_exported_model_forbids_extra():
    models = [
        obj
        for name in contracts.__all__
        if isinstance(obj := getattr(contracts, name), type) and issubclass(obj, BaseModel)
    ]
    assert len(models) > 40
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_duplicate_statement_ids_rejected():
    insight = insight_dict(risks=[statement(["E1"])])
    with pytest.raises(ValidationError, match="повторяются: s1"):
        InsightResult.model_validate(result_dict(insight))


def test_empty_human_text_rejected():
    with pytest.raises(ValidationError):
        Insight.model_validate(insight_dict(headline=""))
    bad = statement(["E1"]) | {"text": ""}
    with pytest.raises(ValidationError):
        Insight.model_validate(insight_dict(what_happened=[bad]))


def test_ref_kind_is_required():
    ref = {k: v for k, v in EVIDENCE["ref"].items() if k != "kind"}
    with pytest.raises(ValidationError):
        NewsRef.model_validate(ref)


def test_feature_key_accepts_tz_spelling():
    for key in ("products.LOA", "products.GAR", "suppliers.metallurgy", "market_position.percentile"):
        Feature(key=key, value=1, source="user", detail=None)
    for key in ("Products.LOA", "products.", "1x"):
        with pytest.raises(ValidationError):
            Feature(key=key, value=1, source="user", detail=None)


def test_defaulted_fields_are_required_in_serialization_schema():
    required = set(contracts.AgentStep.model_json_schema(mode="serialization")["required"])
    assert {"tokens_in", "tokens_out", "cost_rub", "ms", "tool_call"} <= required
    filters = contracts.FeedFilters.model_json_schema(mode="serialization")
    assert {"impact", "factor"} <= set(filters["required"])


def _context(**over) -> InsightContext:
    events = load_fixture("events.json", TypeAdapter(list[Event]))
    profile = next(
        p
        for p in load_fixture("profiles.json", TypeAdapter(list[CompanyProfile]))
        if p.company_id == "C-DEV-01"
    )
    data = {
        "event": events[0],
        "profile": profile_compact(profile, UserRole.cfo),
        "user_role": UserRole.cfo,
        "chains": [],
        "plan": None,
        "evidence": [],
        "spent": Spent(),
    }
    return InsightContext(**(data | over))


def test_insight_context_roundtrip():
    ctx = _context(evidence=[Evidence.model_validate(EVIDENCE)])
    assert InsightContext.model_validate_json(ctx.model_dump_json()) == ctx


def test_insight_context_rejects_duplicate_evidence_ids():
    evidence = Evidence.model_validate(EVIDENCE)
    with pytest.raises(ValidationError, match="повторяются"):
        _context(evidence=[evidence, evidence])


def test_trace_record_accepts_fixture_steps():
    steps = load_fixture("trace.json", TypeAdapter(list[AgentStep]))
    records = [TraceRecord(step=s, context=None) for s in steps]
    assert [r.step for r in records] == steps
    with_context = TraceRecord(step=steps[0], context=_context())
    assert TraceRecord.model_validate_json(with_context.model_dump_json()) == with_context


def test_hypothesis_requires_text():
    with pytest.raises(ValidationError):
        Hypothesis(text="", factor=None, confirm="", refute="")
    assert Hypothesis(text="Ставка давит на кредиты", factor="key_rate", confirm="а", refute="б").factor
