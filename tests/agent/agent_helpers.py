from pydantic import TypeAdapter

from app.clients.profile import profile_compact
from app.contracts import (
    Access,
    CompanyProfile,
    Event,
    MarketSeries,
    SessionCtx,
    SourceFragment,
    ThesisItem,
    UserRole,
)
from app.fixtures import load_fixture

EVENTS = load_fixture("events.json", TypeAdapter(list[Event]))
PROFILES = {p.company_id: p for p in load_fixture("profiles.json", TypeAdapter(list[CompanyProfile]))}
SOURCES = load_fixture("sources.json", TypeAdapter(list[SourceFragment]))
THESES = load_fixture("theses.json", TypeAdapter(list[ThesisItem]))
MARKET = {m.series: m for m in load_fixture("market.json", TypeAdapter(list[MarketSeries]))}


def event(event_id: int) -> Event:
    return next(e for e in EVENTS if e.id == event_id).model_copy(deep=True)


def sources(event_id: int) -> list[SourceFragment]:
    return sorted((s for s in SOURCES if s.event_id == event_id), key=lambda s: s.n)


def session(company_id: str, role: UserRole = UserRole.cfo) -> SessionCtx:
    return SessionCtx(user_id=1, company_id=company_id, user_role=role, access=Access.client)


def compact(company_id: str, role: UserRole = UserRole.cfo):
    return profile_compact(PROFILES[company_id], role)
