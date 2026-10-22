from functools import lru_cache

import yaml

from app.settings import get_settings


@lru_cache
def factor_names() -> dict[str, str]:
    path = get_settings().config_dir / "factors.yaml"
    return {item["id"]: item["name"] for item in yaml.safe_load(path.read_text(encoding="utf-8"))}


@lru_cache
def sector_names() -> dict[str, str]:
    path = get_settings().config_dir / "okved_sectors.yaml"
    sectors = yaml.safe_load(path.read_text(encoding="utf-8"))["sectors"]
    return {item["id"]: item["name"] for item in sectors}


@lru_cache
def factor_ids() -> frozenset[str]:
    return frozenset(factor_names())


@lru_cache
def sector_ids() -> frozenset[str]:
    return frozenset(sector_names())
