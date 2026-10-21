from pydantic import TypeAdapter

from app.contracts import CounterpartyDirection, CounterpartyShare, Exposure, SessionCtx
from app.errors import NotFound
from app.fixtures import load_fixture

_BY_COMPANY = TypeAdapter(dict[str, dict[CounterpartyDirection, list[CounterpartyShare]]])
_SECTOR_EXPOSURES = TypeAdapter(dict[str, list[Exposure]])


# заглушка до DQ-2
async def get_counterparties(
    ctx: SessionCtx, direction: CounterpartyDirection, sector: str | None = None, top: int = 10
) -> list[CounterpartyShare]:
    by_company = load_fixture("counterparties.json", _BY_COMPANY)
    if ctx.company_id not in by_company:
        raise NotFound(f"контрагенты компании {ctx.company_id} не найдены")
    rows = by_company[ctx.company_id].get(direction, [])
    if sector is not None:
        rows = [r for r in rows if r.sector == sector]
    return sorted(rows, key=lambda r: -r.share)[:top]


# заглушка до DQ-7
def get_sector_exposure(sector: str, factor: str) -> Exposure | None:
    rows = load_fixture("sector_exposures.json", _SECTOR_EXPOSURES).get(sector, [])
    return next((e for e in rows if e.factor == factor), None)
