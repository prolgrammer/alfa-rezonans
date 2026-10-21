from pydantic import TypeAdapter

from app.contracts import BankThesis, ThesisItem
from app.fixtures import load_fixture

_THESES = TypeAdapter(list[ThesisItem])


# заглушка до ML-6
async def extract_theses(doc_id: int) -> list[BankThesis]:
    return [t.thesis for t in load_fixture("theses.json", _THESES) if t.doc_id == doc_id]


# заглушка до ML-6
async def get_bank_view(factor: str, limit: int = 3) -> list[ThesisItem]:
    rows = [
        t for t in load_fixture("theses.json", _THESES) if t.verified is True and factor in t.thesis.factors
    ]
    return sorted(rows, key=lambda t: t.published_on, reverse=True)[:limit]
