from decimal import Decimal

from pydantic import JsonValue

from app.clients.profile import feature_phrase
from app.contracts import (
    BankRef,
    Chain,
    CompactProfile,
    Event,
    Evidence,
    EvidenceType,
    Feature,
    FeatureRef,
    InsightContext,
    MarketPoint,
    MarketRef,
    MarketSeries,
    NewsRef,
    SourceFragment,
    Spent,
    ThesisItem,
    UserRole,
)
from app.text.sanitize import neutralize
from app.text.verify import clip

EVIDENCE_TEXT_LIMIT = 1500

type AnyRef = NewsRef | BankRef | MarketRef | FeatureRef


def new_context(
    event: Event, profile: CompactProfile | None, user_role: UserRole, chains: list[Chain]
) -> InsightContext:
    return InsightContext(
        event=event,
        profile=profile,
        user_role=user_role,
        chains=chains,
        plan=None,
        evidence=[],
        spent=Spent(),
    )


def add_evidence(ctx: InsightContext, type: EvidenceType, text: str, ref: AnyRef) -> str | None:
    """Единственный способ добавить доказательство; None — ссылаться не на что, утверждение пропускают."""
    # текст уйдёт в промпты как <source n="E7">, поэтому очищаем его здесь, а не у вызывающего
    clean = clip(neutralize(text).text, EVIDENCE_TEXT_LIMIT)
    if not clean:
        return None
    # одна публикация даёт несколько доказательств с разными фрагментами, поэтому ключ — пара, а не ref
    key = (ref.model_dump_json(), clean)
    for existing in ctx.evidence:
        if (existing.ref.model_dump_json(), existing.text) == key:
            return existing.id
    # максимум, а не длина: снимок мог быть собран не этим кодом
    number = max((int(e.id[1:]) for e in ctx.evidence), default=0) + 1
    evidence_id = f"E{number}"
    ctx.evidence.append(Evidence(id=evidence_id, type=type, text=clean, ref=ref))
    return evidence_id


def news_evidence(ctx: InsightContext, fragment: SourceFragment, text: str | None = None) -> str | None:
    ref = NewsRef(
        kind="news",
        event_id=fragment.event_id,
        raw_item_id=fragment.raw_item_id,
        url=fragment.url,
        source_name=fragment.source_name,
        published_at=fragment.published_at,
    )
    return add_evidence(ctx, EvidenceType.news, fragment.text if text is None else text, ref)


def bank_evidence(ctx: InsightContext, item: ThesisItem) -> str | None:
    ref = BankRef(
        kind="bank",
        doc_id=item.doc_id,
        page=item.thesis.page,
        thesis_id=item.id,
        published_on=item.published_on,
    )
    # цитата стоит на странице дословно, а пересказ тезиса — нет
    return add_evidence(ctx, EvidenceType.bank, item.thesis.quote, ref)


def market_evidence(ctx: InsightContext, series: MarketSeries, point: MarketPoint) -> str | None:
    # str(float) сохраняет запись данных («14.0»), format(..., "f") не уходит в экспоненту
    value = format(Decimal(str(point.value)), "f").replace(".", ",")
    unit = series.unit if series.unit == "%" else f" {series.unit}" if series.unit else ""
    text = f"{series.name} на {point.date:%d.%m.%Y}: {value}{unit}."
    return add_evidence(
        ctx, EvidenceType.market, text, MarketRef(kind="market", series=series.series, date=point.date)
    )


def feature_evidence(ctx: InsightContext, feature: Feature) -> str | None:
    # V6 требует истинный признак: «products.DEP: False» ничего не подтверждает
    if not is_true_value(feature.value):
        return None
    ref = FeatureRef(kind="feature", feature_key=feature.key, source=feature.source)
    return add_evidence(ctx, EvidenceType.feature, feature_phrase(feature.key, feature.value), ref)


def is_true_value(value: JsonValue) -> bool:
    return bool(value)
