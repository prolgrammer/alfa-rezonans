from functools import lru_cache

import yaml

from app.settings import get_settings


@lru_cache
def factor_ids() -> frozenset[str]:
    path = get_settings().config_dir / "factors.yaml"
    return frozenset(item["id"] for item in yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def sector_ids() -> frozenset[str]:
    path = get_settings().config_dir / "okved_sectors.yaml"
    return frozenset(item["id"] for item in yaml.safe_load(path.read_text(encoding="utf-8"))["sectors"])
