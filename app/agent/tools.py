import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.bank.theses import get_bank_view
from app.clients.counterparties import get_counterparties, get_sector_exposure
from app.clients.profile import get_profile
from app.collect.market import get_market_series
from app.contracts import (
    BankChunk,
    CounterpartyDirection,
    Insight,
    ProfileSection,
    SessionCtx,
    ThesisItem,
)
from app.contracts._base import ContractModel
from app.contracts.types import Slug
from app.news.search import get_event_sources, search_bank_chunks, search_news

# Признаки раздела «обзор» — всё, что не относится к остальным разделам.
_OVERVIEW_EXCLUDED = ("products.", "group.", "regions")


class GetCompanyProfileArgs(ContractModel):
    section: ProfileSection


class GetCounterpartiesArgs(ContractModel):
    direction: CounterpartyDirection
    sector: Slug | None = None
    top: int = Field(default=10, ge=1, le=10)


class GetBankViewArgs(ContractModel):
    factor: Slug


class SearchBankResearchArgs(ContractModel):
    query: str = Field(max_length=200)
    factor: Slug | None = None
    since: dt.date | None = None


class GetMarketDataArgs(ContractModel):
    series: Slug
    days: int = Field(default=90, ge=1, le=365)


class SearchNewsArgs(ContractModel):
    query: str = Field(max_length=200)
    days: int = Field(default=30, ge=1, le=90)
    factor: Slug | None = None


class GetEventSourcesArgs(ContractModel):
    hint: Annotated[str, Field(max_length=200)] | None = None


class GetSectorExposureArgs(ContractModel):
    sector: Slug
    factor: Slug


class SubmitInsightArgs(ContractModel):
    draft: Insight


class BankResearch(ContractModel):
    theses: list[ThesisItem]
    chunks: list[BankChunk]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any], Awaitable[Any]]


def _in_section(key: str, section: ProfileSection) -> bool:
    if section == ProfileSection.overview:
        return not key.startswith(_OVERVIEW_EXCLUDED)
    return key == section.value or key.startswith(f"{section.value}.")


def build_tools(ctx: SessionCtx, event_id: int) -> dict[str, Tool]:
    # Компания и событие замкнуты здесь: у моделей аргументов нет company_id, запросить чужое нельзя.
    async def company_profile(a: GetCompanyProfileArgs) -> Any:
        profile = await get_profile(ctx.company_id)
        if a.section == ProfileSection.exposures:
            return profile.exposures
        return [f for f in profile.features if _in_section(f.key, a.section)]

    async def counterparties(a: GetCounterpartiesArgs) -> Any:
        return await get_counterparties(ctx, a.direction, a.sector, a.top)

    async def bank_view(a: GetBankViewArgs) -> Any:
        return await get_bank_view(a.factor)

    async def bank_research(a: SearchBankResearchArgs) -> BankResearch:
        theses = await get_bank_view(a.factor) if a.factor else []
        return BankResearch(theses=theses, chunks=await search_bank_chunks(a.query, a.factor, a.since))

    async def market_data(a: GetMarketDataArgs) -> Any:
        return await get_market_series(a.series, a.days)

    async def news(a: SearchNewsArgs) -> Any:
        return await search_news(a.query, a.days, a.factor)

    async def event_sources(a: GetEventSourcesArgs) -> Any:
        return await get_event_sources(event_id, a.hint)

    async def sector_exposure(a: GetSectorExposureArgs) -> Any:
        return get_sector_exposure(a.sector, a.factor)

    async def submit_insight(a: SubmitInsightArgs) -> Insight:
        return a.draft

    tools = [
        Tool(
            "get_company_profile",
            "Признаки компании по разделу: обзор, продукты, группа, регионы или чувствительности",
            GetCompanyProfileArgs,
            company_profile,
        ),
        Tool(
            "get_counterparties",
            "Отрасли поставщиков или покупателей с долями и регионами",
            GetCounterpartiesArgs,
            counterparties,
        ),
        Tool(
            "get_bank_view",
            "Свежие проверенные тезисы банка по фактору",
            GetBankViewArgs,
            bank_view,
        ),
        Tool(
            "search_bank_research",
            "Тезисы и фрагменты аналитики банка по запросу",
            SearchBankResearchArgs,
            bank_research,
        ),
        Tool(
            "get_market_data",
            "Значения и изменения ряда рыночных данных",
            GetMarketDataArgs,
            market_data,
        ),
        Tool(
            "search_news",
            "Связанные события: предыстория, прошлые решения, реакция рынка",
            SearchNewsArgs,
            news,
        ),
        Tool(
            "get_event_sources",
            "Фрагменты публикаций текущего события по подсказке",
            GetEventSourcesArgs,
            event_sources,
        ),
        Tool(
            "get_sector_exposure",
            "Строка матрицы чувствительностей отрасли к фактору",
            GetSectorExposureArgs,
            sector_exposure,
        ),
        Tool(
            "submit_insight",
            "Передать итоговый разбор по схеме",
            SubmitInsightArgs,
            submit_insight,
        ),
    ]
    return {t.name: t for t in tools}
