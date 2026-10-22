import datetime as dt
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal

from app.agent.context import bank_evidence, feature_evidence, new_context, news_evidence
from app.agent.guard import bounded
from app.agent.links import (
    FactorMove,
    build_chains,
    effect_text,
    event_moves,
    feature_key,
    link_text,
)
from app.agent.trace import TraceSink
from app.bank.theses import get_bank_view
from app.catalogs import factor_names
from app.clients.profile import get_profile, profile_compact
from app.contracts import (
    AgentStep,
    BankView,
    Chain,
    CompanyProfile,
    Confidence,
    Event,
    ForecastMatch,
    Horizon,
    Impact,
    ImpactDirection,
    Insight,
    InsightChecks,
    InsightContext,
    InsightMode,
    InsightResult,
    Link,
    Magnitude,
    Origin,
    SessionCtx,
    SourceFragment,
    Statement,
    StepKind,
    ThesisItem,
    ThesisKind,
    ToolCallRecord,
)
from app.errors import NotFound
from app.llm import AllModelsFailed, BudgetExceeded, LLMNotConfigured
from app.news.search import get_event_sources
from app.text.verify import clip, date_mentioned, date_words, find_quote, mark_in, number_marks

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 3.0
BANK_PER_FACTOR = 3

STATEMENT_LIMIT = 400
HEADLINE_LIMIT = 110
LIMITATION_LIMIT = 300
FEATURES_PER_LINK = 5
_WHAT_LIMIT = 6
_ASSUMPTIONS_LIMIT = 6
_WHY_LIMIT = 6
_RISKS_LIMIT = 6
_WATCH_LIMIT = 4
_LIMITATIONS_MAX = 5

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_STRENGTH = {1: Magnitude.low, 2: Magnitude.medium, 3: Magnitude.high}
_SIGN_MARK = {-1: "−", 0: "0", 1: "+"}

type BankViewFn = Callable[[str, int], Awaitable[list[ThesisItem]]]


def reason_text(error: BaseException | None) -> str:
    if isinstance(error, LLMNotConfigured):
        return "модель не подключена"
    if isinstance(error, BudgetExceeded):
        return "исчерпан дневной бюджет на модели"
    if isinstance(error, AllModelsFailed):
        return "модели не ответили"
    return "модель недоступна"


def event_factor_order(event: Event) -> list[str]:
    order: dict[str, None] = {}
    for factor in [*(f.id for f in event.card.factors), *event.factors] if event.card else event.factors:
        order.setdefault(factor)
    return list(order)


def _factor_name(factor: str) -> str:
    return factor_names().get(factor, factor)


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _sentence(text: str) -> str:
    text = _capitalize(text)
    return text if text.endswith((".", "!", "?")) else text + "."


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


class _Builder:
    """Собирает утверждения: id и доказательства выдаются только тем, что реально попали в разбор."""

    def __init__(self, ctx: InsightContext, profile: CompanyProfile | None, sources: list[SourceFragment]):
        self.ctx = ctx
        self.features = {f.key: f for f in profile.features} if profile else {}
        self.fragments = sorted(sources, key=lambda s: s.n)
        self.by_n = {s.n: s for s in self.fragments}
        self.moves: dict[str, FactorMove] = {m.factor: m for m in event_moves(ctx.event)}
        self._count = 0

    def next_id(self) -> str:
        self._count += 1
        return f"s{self._count}"

    def quote_evidence(self, fragment: SourceFragment, quote: str, statement_text: str) -> str | None:
        # ссылка точнее, когда это предложение с цитатой и всеми числами утверждения: иначе V4 их не найдёт
        marks = number_marks(statement_text)
        for sentence in _sentences(fragment.text):
            if find_quote(quote, {0: sentence}):
                have = number_marks(sentence)
                if all(mark_in(m, have) for m in marks):
                    return news_evidence(self.ctx, fragment, sentence)
        return news_evidence(self.ctx, fragment)

    def cited(self, cites: list[int], quote: str, statement_text: str) -> list[str]:
        found: list[str] = []
        for n in cites:
            fragment = self.by_n.get(n)
            if fragment is None:
                continue
            evidence_id = self.quote_evidence(fragment, quote, statement_text)
            if evidence_id is not None and evidence_id not in found:
                found.append(evidence_id)
        return found

    def card_statements(
        self, items: list[tuple[str, list[int], str]], limit: int, confidence: Confidence
    ) -> list[Statement]:
        block: list[Statement] = []
        for text, cites, quote in items:
            if len(block) >= limit:
                break
            text = clip(text, STATEMENT_LIMIT)
            evidence = self.cited(cites, quote, text)
            if evidence:
                block.append(
                    Statement(
                        id=self.next_id(),
                        text=text,
                        origin=Origin.fact,
                        evidence=evidence,
                        confidence=confidence,
                    )
                )
        return block

    def chain_evidence(self, chain: Chain, statement_text: str) -> tuple[list[str], list[str]]:
        evidence: list[str] = []
        move = self.moves.get(chain.factor)
        if move is not None and move.quote:
            fragment = next((f for f in self.fragments if find_quote(move.quote, {0: f.text})), None)
            if fragment is not None and (eid := self.quote_evidence(fragment, move.quote, statement_text)):
                evidence.append(eid)
        keys: list[str] = []
        for because in chain.because:
            key = feature_key(because)
            feature = self.features.get(key)
            if key in keys or feature is None or len(keys) >= FEATURES_PER_LINK:
                continue
            if (eid := feature_evidence(self.ctx, feature)) is not None:
                keys.append(key)
                if eid not in evidence:
                    evidence.append(eid)
        return evidence, keys

    def date_evidence(self, date: dt.date) -> str | None:
        for fragment in self.fragments:
            for sentence in _sentences(fragment.text):
                if date_mentioned(date, sentence):
                    return news_evidence(self.ctx, fragment, sentence)
            if date_mentioned(date, fragment.text):
                return news_evidence(self.ctx, fragment)
        return None

    def evidence_text(self, evidence_id: str) -> str:
        return next(e.text for e in self.ctx.evidence if e.id == evidence_id)


def _date_phrase(date: dt.date, evidence_text: str) -> str:
    # источник нередко пишет «на 15 октября» без года: год из карточки в утверждении V4 не нашёл бы
    full = date_words(date)
    if mark_in((Decimal(date.year), None, None), number_marks(evidence_text)):
        return full
    return full.rsplit(" ", 2)[0]


def _chain_confidence(chain: Chain) -> Confidence:
    return Confidence.medium if chain.strength == 3 else Confidence.low


def _headline(ctx: InsightContext) -> str:
    if ctx.event.card is None:
        return "Карточка события ещё не готова: краткий разбор по факторам события"
    if not ctx.chains:
        return "Прямой связи события с профилем компании не найдено"
    effects: list[str] = []
    for chain in ctx.chains:
        if chain.expected_effect in (ImpactDirection.risk, ImpactDirection.opportunity):
            text = effect_text(chain)
            if text not in effects:
                effects.append(text)
        if len(effects) == 2:
            break
    if effects:
        return clip(_capitalize(", ".join(effects)), HEADLINE_LIMIT)
    first = ctx.chains[0]
    name = _factor_name(first.factor)
    if first.expected_effect == ImpactDirection.mixed:
        return clip(f"{name}: влияние на компанию неоднозначно", HEADLINE_LIMIT)
    return clip(f"{name}: знак влияния не определён", HEADLINE_LIMIT)


def _impact(ctx: InsightContext) -> Impact:
    signed = [c for c in ctx.chains if c.expected_effect != ImpactDirection.neutral]
    direction = ImpactDirection.neutral
    if signed:
        top = max(c.strength for c in signed)
        effects = {c.expected_effect for c in signed if c.strength == top}
        direction = effects.pop() if len(effects) == 1 else ImpactDirection.mixed
    strength = _STRENGTH[ctx.chains[0].strength] if ctx.chains else Magnitude.low
    horizon = ctx.event.card.horizon if ctx.event.card else Horizon.months
    return Impact(direction=direction, strength=strength, horizon=horizon)


def _forecast_match(event: Event, theses: list[ThesisItem], factor: str | None) -> ForecastMatch:
    if event.card is None or factor is None:
        return ForecastMatch.no_forecast
    card_factor = next((f for f in event.card.factors if f.id == factor), None)
    if card_factor is None or card_factor.direction == "unclear":
        return ForecastMatch.no_forecast
    # прогноз, вышедший после события, мог учесть его, и сравнивать с ним нечестно
    before = event.first_seen_at.date()
    forecasts = [
        t
        for t in theses
        if t.thesis.kind == ThesisKind.forecast
        and t.published_on < before
        and t.thesis.direction != "unclear"
    ]
    if not forecasts:
        return ForecastMatch.no_forecast
    same = all(t.thesis.direction == card_factor.direction for t in forecasts)
    return ForecastMatch.matches if same else ForecastMatch.partly


def build_brief(
    ctx: InsightContext,
    *,
    profile: CompanyProfile | None,
    sources: list[SourceFragment] | None,
    theses: list[ThesisItem],
    theses_factor: str | None,
    bank_failed: bool,
    reason: str,
) -> Insight:
    b = _Builder(ctx, profile, sources or [])
    card = ctx.event.card

    what: list[Statement] = []
    assumptions: list[Statement] = []
    watch: list[Statement] = []
    if card is not None:
        what = b.card_statements(
            [(f.text, f.cites, f.quote) for f in card.facts] + [(c.text, c.cites, "") for c in card.causes],
            _WHAT_LIMIT,
            Confidence.high,
        )
        assumptions = b.card_statements(
            [(f"{c.text.rstrip('.')} (источник оценки — {c.who}).", c.cites, c.quote) for c in card.claims],
            _ASSUMPTIONS_LIMIT,
            Confidence.medium,
        )

    why: list[Link] = []
    for chain in ctx.chains:
        if len(why) >= _WHY_LIMIT:
            break
        text = clip(link_text(chain), STATEMENT_LIMIT)
        evidence, keys = b.chain_evidence(chain, text)
        if evidence:
            why.append(
                Link(
                    id=b.next_id(),
                    text=text,
                    origin=Origin.model,
                    evidence=evidence,
                    confidence=_chain_confidence(chain),
                    feature_keys=keys,
                )
            )

    def effects(kind: ImpactDirection) -> list[Statement]:
        block: list[Statement] = []
        for chain in ctx.chains:
            if len(block) >= _RISKS_LIMIT:
                break
            text = clip(_sentence(effect_text(chain)), STATEMENT_LIMIT)
            if chain.expected_effect != kind or any(s.text == text for s in block):
                continue
            evidence, _ = b.chain_evidence(chain, text)
            if evidence:
                block.append(
                    Statement(
                        id=b.next_id(),
                        text=text,
                        origin=Origin.model,
                        evidence=evidence,
                        confidence=_chain_confidence(chain),
                    )
                )
        return block

    risks = effects(ImpactDirection.risk)
    opportunities = effects(ImpactDirection.opportunity)

    if card is not None:
        for item in card.dates:
            if len(watch) >= _WATCH_LIMIT:
                break
            if evidence_id := b.date_evidence(item.date):
                phrase = _date_phrase(item.date, b.evidence_text(evidence_id))
                text = clip(_sentence(f"{item.what}: {phrase}"), STATEMENT_LIMIT)
                watch.append(
                    Statement(
                        id=b.next_id(),
                        text=text,
                        origin=Origin.fact,
                        evidence=[evidence_id],
                        confidence=Confidence.high,
                    )
                )

    shown: list[ThesisItem] = []
    bank_statements: list[Statement] = []
    for item in theses[:BANK_PER_FACTOR]:
        text = clip(item.thesis.text, STATEMENT_LIMIT)
        if evidence_id := bank_evidence(ctx, item):
            shown.append(item)
            bank_statements.append(
                Statement(
                    id=b.next_id(),
                    text=text,
                    origin=Origin.bank,
                    evidence=[evidence_id],
                    confidence=Confidence.high,
                )
            )

    factors = event_factor_order(ctx.event)
    main = factors[0] if factors else None
    if bank_statements:
        bank_view = BankView(
            statements=bank_statements,
            as_of=max(t.published_on for t in shown),
            match=_forecast_match(ctx.event, shown, theses_factor),
            gap=None,
        )
    else:
        subject = f"по фактору «{_factor_name(main)}»" if main else "по факторам события"
        gap = (
            f"Позиция банка {subject} сейчас недоступна"
            if bank_failed
            else f"У банка нет вычитанной позиции {subject}"
        )
        bank_view = BankView(statements=[], as_of=None, match=ForecastMatch.no_forecast, gap=gap)

    limitations = [
        f"Краткий разбор: {reason}. Собран по шаблону из карточки события, цепочек влияния и позиции банка, "
        "без модели."
    ]
    if card is None:
        limitations.append(
            "Карточка события ещё не готова: направление факторов неизвестно, знак влияния не определён."
        )
    if profile is None:
        limitations.append("Профиль компании недоступен: связь события с компанией не оценена.")
    elif not ctx.chains:
        limitations.append("Среди чувствительностей компании нет факторов этого события.")
    if not sources:
        limitations.append("Публикации события недоступны: утверждения без опоры в источниках не показаны.")
    if main is not None:
        name = _factor_name(main)
        if bank_failed:
            limitations.append(f"Позиция банка недоступна: чтение тезисов по фактору «{name}» не удалось.")
        elif not bank_statements:
            limitations.append(f"У банка нет вычитанной позиции по главному фактору «{name}».")
        elif theses_factor is not None and theses_factor != main:
            limitations.append(
                f"По главному фактору «{name}» позиции банка нет: показана позиция по фактору "
                f"«{_factor_name(theses_factor)}»."
            )

    return Insight(
        headline=_headline(ctx),
        impact=_impact(ctx),
        what_happened=what,
        assumptions=assumptions,
        why_it_matters=why,
        risks=risks,
        opportunities=opportunities,
        watch=watch,
        scenarios=[],
        actions=[],
        questions=[],
        bank_view=bank_view,
        bank_offer=None,
        confidence=Confidence.low if card is None or not ctx.chains else Confidence.medium,
        limitations=[clip(x, LIMITATION_LIMIT) for x in limitations[:_LIMITATIONS_MAX]],
    )


def brief_result(ctx: InsightContext, insight: Insight) -> InsightResult:
    used = {e for s in insight.statements() for e in s.evidence}
    statements = insight.statements()
    return InsightResult(
        insight=insight,
        evidence=[e for e in ctx.evidence if e.id in used],
        mode=InsightMode.brief,
        # V1–V10 краткий разбор не проходит, поэтому «все прошли» было бы неправдой
        checks=InsightChecks(statements_total=len(statements), passed_first_try=0, removed=0, revisions=0),
    )


async def _read[T](what: str, call: Awaitable[T]) -> tuple[T | None, Exception | None]:
    try:
        return await bounded(call, READ_TIMEOUT_S), None
    except NotFound as exc:
        log.info("краткий разбор: %s — нет данных: %s", what, exc)
        return None, exc
    except Exception as exc:
        # последняя линия обороны: недоступная БД не должна оставлять экран пустым
        log.warning("краткий разбор: %s не прочитано: %r", what, exc)
        return None, exc


def _error_text(error: Exception) -> str:
    return clip(str(error) or type(error).__name__, 300)


def _chains_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "цепочка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "цепочки"
    return "цепочек"


def _chains_output(chains: list[Chain]) -> str:
    if not chains:
        return "цепочек нет"
    parts = []
    for chain in chains:
        mark = "±" if chain.event_direction == "volatile" else _SIGN_MARK[chain.company_sign]
        parts.append(f"{_factor_name(chain.factor)} × {chain.channel} ({mark})")
    return f"{len(chains)} {_chains_word(len(chains))}: " + "; ".join(parts)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def run_brief(
    session: SessionCtx,
    event: Event,
    *,
    sink: TraceSink,
    error: BaseException | None,
    bank_view: BankViewFn = get_bank_view,
) -> AsyncIterator[AgentStep | InsightResult]:
    reason = reason_text(error)
    log.info("краткий разбор события %s: %r", event.id, error)
    ctx = new_context(event, None, session.user_role, [])
    factors = event_factor_order(event)

    yield await sink.record(
        ctx,
        StepKind.fallback,
        "Модель недоступна — собираю краткий разбор…",
        reason,
        "разбор по шаблону из карточки, цепочек влияния и позиции банка",
    )

    started = time.perf_counter()
    profile, _ = await _read("профиль компании", get_profile(session.company_id))
    if profile is not None:
        ctx.profile = profile_compact(profile, session.user_role)
        ctx.chains = build_chains(event_moves(event), profile.exposures)
    ms = _elapsed_ms(started)
    ctx.spent.ms += ms
    yield await sink.record(
        ctx,
        StepKind.links,
        "Сопоставляю факторы события с вашим профилем…",
        f"событие {event.id}; факторы: {', '.join(factors) or 'нет'}",
        _chains_output(ctx.chains),
        ms=ms,
    )

    started = time.perf_counter()
    fragments, failure = await _read("публикации события", get_event_sources(event.id))
    ms = _elapsed_ms(started)
    ctx.spent.ms += ms
    ctx.spent.tool_calls += 1
    yield await sink.record(
        ctx,
        StepKind.tool,
        "Читаю публикации события…",
        "get_event_sources()",
        f"фрагментов: {len(fragments)}" if fragments is not None else "публикации недоступны",
        ms=ms,
        tool_call=ToolCallRecord(
            tool="get_event_sources",
            args={},
            result_chars=sum(len(f.text) for f in fragments or []),
            duration_ms=ms,
            ok=failure is None,
            error=_error_text(failure) if failure else None,
        ),
    )

    theses: list[ThesisItem] = []
    theses_factor: str | None = None
    bank_failed = False
    for factor in factors:
        started = time.perf_counter()
        found, failure = await _read(f"позиция банка по {factor}", bank_view(factor, BANK_PER_FACTOR))
        ms = _elapsed_ms(started)
        ctx.spent.ms += ms
        ctx.spent.tool_calls += 1
        output = "позиция недоступна" if failure else (f"тезисов: {len(found)}" if found else "тезисов нет")
        yield await sink.record(
            ctx,
            StepKind.tool,
            f"Смотрю позицию банка по фактору «{_factor_name(factor)}»…",
            f"get_bank_view(factor={factor})",
            output,
            ms=ms,
            tool_call=ToolCallRecord(
                tool="get_bank_view",
                args={"factor": factor, "limit": BANK_PER_FACTOR},
                result_chars=sum(len(t.thesis.text) + len(t.thesis.quote) for t in found or []),
                duration_ms=ms,
                ok=failure is None,
                error=_error_text(failure) if failure else None,
            ),
        )
        if failure is not None:
            bank_failed = True
            break
        if found:
            theses, theses_factor = found, factor
            break

    insight = build_brief(
        ctx,
        profile=profile,
        sources=fragments,
        theses=theses,
        theses_factor=theses_factor,
        bank_failed=bank_failed,
        reason=reason,
    )
    result = brief_result(ctx, insight)
    yield await sink.record(
        ctx,
        StepKind.final,
        "Краткий разбор готов",
        "",
        f"утверждений: {result.checks.statements_total}, доказательств: {len(result.evidence)}",
    )
    await sink.flush()
    yield result
