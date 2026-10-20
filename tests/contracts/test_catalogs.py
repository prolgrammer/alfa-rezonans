import yaml

from app.catalogs import factor_ids, sector_ids
from app.settings import get_settings

FACTORS = {
    "денежно-кредитные": ["key_rate", "inflation", "ofz_yields", "credit_availability"],
    "валюта": ["usd_rub", "eur_rub", "cny_rub", "fx_control"],
    "сырьё": [
        "oil", "gas", "coal", "steel", "aluminum", "copper", "nickel", "gold", "grain", "fertilizers",
        "timber",
    ],
    "спрос": ["consumer_demand", "investment_demand", "gdp_growth", "mortgage_housing", "public_procurement"],
    "издержки": ["wages_labor", "energy_tariffs", "fuel", "logistics_freight"],
    "регулирование": ["taxes", "export_duties", "import_duties", "labeling", "sector_regulation"],
    "внешние": ["sanctions", "cross_border_payments", "china_trade"],
}  # fmt: skip

SECTORS = [
    "agriculture", "food", "coal_mining", "oil_gas", "metal_ore_mining", "metallurgy", "chemicals",
    "fertilizers_prod", "pharma", "timber_paper", "construction_materials", "machinery", "automotive",
    "electronics_electrical", "light_industry", "power", "utilities", "development", "construction",
    "auto_trade", "wholesale", "retail", "transport_logistics", "hospitality", "media", "telecom", "it",
    "finance", "real_estate", "professional_services", "public_admin", "education", "healthcare",
    "culture_sport", "other_services",
]  # fmt: skip


def _load(name: str):
    return yaml.safe_load((get_settings().config_dir / name).read_text(encoding="utf-8"))


def test_factors_match_spec():
    items = _load("factors.yaml")
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)) == 36
    assert set(ids) == {f for group in FACTORS.values() for f in group}
    assert factor_ids() == frozenset(ids)
    for item in items:
        assert item["name"]
        assert item["id"] in FACTORS[item["group"]]


def test_sectors_match_spec():
    items = _load("okved_sectors.yaml")["sectors"]
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)) == 35
    assert set(ids) == set(SECTORS)
    assert sector_ids() == frozenset(ids)
    assert all(i["name"] for i in items)
