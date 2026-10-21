from enum import StrEnum


class EventType(StrEnum):
    regulator_decision = "regulator_decision"
    macro_stats = "macro_stats"
    prices_markets = "prices_markets"
    currency = "currency"
    taxes_law = "taxes_law"
    sanctions_trade = "sanctions_trade"
    sector = "sector"
    corporate = "corporate"
    infrastructure_logistics = "infrastructure_logistics"
    emergency = "emergency"
    other = "other"


class Certainty(StrEnum):
    happened = "happened"
    decided = "decided"
    planned = "planned"
    rumor = "rumor"


class Horizon(StrEnum):
    days = "days"
    weeks = "weeks"
    months = "months"
    years = "years"


class FactorDirection(StrEnum):
    """Куда движется фактор в событии."""

    up = "up"
    down = "down"
    flat = "flat"
    volatile = "volatile"
    unclear = "unclear"


class Magnitude(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class SectorRole(StrEnum):
    affected = "affected"
    beneficiary = "beneficiary"
    source = "source"


class ImpactDirection(StrEnum):
    risk = "risk"
    opportunity = "opportunity"
    mixed = "mixed"
    neutral = "neutral"


class Origin(StrEnum):
    fact = "fact"
    bank = "bank"
    model = "model"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class EvidenceType(StrEnum):
    news = "news"
    bank = "bank"
    market = "market"
    feature = "feature"


class ThesisKind(StrEnum):
    forecast = "forecast"
    assessment = "assessment"
    fact = "fact"
    recommendation = "recommendation"


class FeatureSource(StrEnum):
    csv = "csv"
    calc = "calc"
    group = "group"
    user = "user"


class SourceKind(StrEnum):
    official = "official"
    news = "news"
    industry = "industry"


class SourceType(StrEnum):
    rss = "rss"
    html = "html"
    api = "api"


class UserRole(StrEnum):
    """Роль человека в компании клиента."""

    owner = "owner"
    ceo = "ceo"
    cfo = "cfo"
    treasurer = "treasurer"
    chief_accountant = "chief_accountant"
    other = "other"


class Access(StrEnum):
    """Права в сервисе."""

    client = "client"
    expert = "expert"
    admin = "admin"


class StepKind(StrEnum):
    links = "links"
    plan = "plan"
    tool = "tool"
    draft = "draft"
    check = "check"
    critic = "critic"
    revise = "revise"
    final = "final"
    fallback = "fallback"


class FeedbackVerdict(StrEnum):
    useful = "useful"
    miss = "miss"


class FeedbackReason(StrEnum):
    not_my_topic = "not_my_topic"
    already_knew = "already_knew"
    factual_error = "factual_error"
    wrong_conclusion = "wrong_conclusion"


class RawItemStatus(StrEnum):
    new = "new"
    filtered_out = "filtered_out"
    clustered = "clustered"


class CardStatus(StrEnum):
    none = "none"
    pending = "pending"
    ok = "ok"
    failed = "failed"


class InsightStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class InsightMode(StrEnum):
    full = "full"
    brief = "brief"


class ExpertAction(StrEnum):
    accept = "accept"
    edit = "edit"
    reject = "reject"


class ChatAuthor(StrEnum):
    user = "user"
    assistant = "assistant"


class OverrideOrigin(StrEnum):
    edit = "edit"
    text = "text"


class RelevanceStage(StrEnum):
    shown = "shown"
    below_threshold = "below_threshold"
    duplicate = "duplicate"
    excluded = "excluded"
    diversity_cut = "diversity_cut"


class ForecastMatch(StrEnum):
    matches = "matches"
    contradicts = "contradicts"
    partly = "partly"
    no_forecast = "no_forecast"


class Period(StrEnum):
    day = "day"
    week = "week"
    month = "month"


class FeedFilter(StrEnum):
    all = "all"
    risk = "risk"
    opportunity = "opportunity"


class CounterpartyDirection(StrEnum):
    suppliers = "suppliers"
    customers = "customers"


class ProfileSection(StrEnum):
    overview = "overview"
    products = "products"
    group = "group"
    regions = "regions"
    exposures = "exposures"
