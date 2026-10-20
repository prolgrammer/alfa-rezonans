from collections.abc import Mapping
from typing import Any

from app.contracts import CompanyProfile, Exposure


# заглушка до DQ-7
def exposures(profile: CompanyProfile, config: Mapping[str, Any]) -> list[Exposure]:
    return profile.exposures
