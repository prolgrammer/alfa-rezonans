from pydantic import JsonValue, TypeAdapter

from app.contracts import CompactProfile, CompanyProfile, UserRole
from app.fixtures import load_fixture

_PROFILES = TypeAdapter(list[CompanyProfile])

TOP_EXPOSURES = 8
COMPACT_FEATURES = 20


# заглушка до DQ-2
async def get_profile(company_id: str) -> CompanyProfile:
    for profile in load_fixture("profiles.json", _PROFILES):
        if profile.company_id == company_id:
            return profile
    raise LookupError(f"профиль компании {company_id} не найден")


# заглушка до DQ-2
def profile_compact(profile: CompanyProfile, user_role: UserRole) -> CompactProfile:
    features = {f.key: f.value for f in profile.features}
    # sorted устойчив: при равной силе порядок чувствительностей из профиля сохраняется
    top = sorted(profile.exposures, key=lambda e: -e.strength)[:TOP_EXPOSURES]
    sector, sector_name, scale = features.get("sector"), features.get("sector_name"), features.get("scale")
    return CompactProfile(
        sector=sector if isinstance(sector, str) else None,
        sector_name=sector_name if isinstance(sector_name, str) else None,
        scale=scale if isinstance(scale, str) else None,
        user_role=user_role,
        completeness=profile.completeness,
        top_exposures=top,
        features=profile.features[:COMPACT_FEATURES],
    )


# заглушка до DQ-2
def feature_phrase(key: str, value: JsonValue) -> str:
    return f"{key}: {value}"
