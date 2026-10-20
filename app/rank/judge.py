from app.contracts import CompactProfile, EventCard, JudgeScore


# заглушка до ML-7
async def rank_judge(profile: CompactProfile, cards: list[tuple[int, EventCard]]) -> list[JudgeScore]:
    # пустой список — формула считает ленту без судьи
    return []
