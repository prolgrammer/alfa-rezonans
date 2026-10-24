import re

import pytest
from agent_helpers import PROFILES, event

from app.agent.links import (
    FactorMove,
    build_chains,
    chain_strength,
    channel_texts,
    direction_noun,
    effect_text,
    event_moves,
    expected_effect,
    feature_key,
    find_channel,
    link_text,
)
from app.catalogs import factor_ids, factor_names
from app.contracts import Chain, Exposure, FactorDirection, Magnitude


def exposure(factor="key_rate", sign=-1, strength=2, channels=("канал",), because=("sector=x",)) -> Exposure:
    return Exposure(
        factor=factor, sign=sign, strength=strength, channels=list(channels), because=list(because)
    )


def move(factor="key_rate", direction="flat", magnitude="high", quote=None) -> FactorMove:
    return FactorMove(factor, FactorDirection(direction), Magnitude(magnitude), quote)


@pytest.mark.parametrize(
    ("direction", "sign", "effect"),
    [
        ("up", 1, "opportunity"),
        ("up", -1, "risk"),
        ("up", 0, "neutral"),
        ("down", 1, "risk"),
        ("down", -1, "opportunity"),
        ("down", 0, "neutral"),
        ("flat", 1, "opportunity"),
        ("flat", -1, "risk"),
        ("flat", 0, "neutral"),
        ("volatile", 1, "mixed"),
        ("volatile", -1, "mixed"),
        ("volatile", 0, "neutral"),
        ("unclear", 1, "neutral"),
        ("unclear", -1, "neutral"),
        ("unclear", 0, "neutral"),
    ],
)
def test_expected_effect_table(direction, sign, effect):
    assert expected_effect(FactorDirection(direction), sign) == effect


@pytest.mark.parametrize(
    ("strength", "magnitude", "expected"),
    [
        (1, "low", 1),
        (1, "medium", 1),
        (1, "high", 2),
        (2, "low", 1),
        (2, "medium", 2),
        (2, "high", 3),
        (3, "low", 2),
        (3, "medium", 3),
        (3, "high", 3),
    ],
)
def test_chain_strength_table(strength, magnitude, expected):
    assert chain_strength(strength, Magnitude(magnitude)) == expected


def test_case_example_developer():
    dev = PROFILES["C-DEV-01"]
    chains = build_chains(event_moves(event(1)), dev.exposures)
    by_channel = {c.channel: c for c in chains if c.factor == "key_rate"}
    loa = by_channel["стоимость кредитов"]
    assert (loa.expected_effect, loa.company_sign, loa.strength, loa.because) == (
        "risk",
        -1,
        3,
        ["products.LOA"],
    )
    assert effect_text(loa) == "процентная нагрузка не снизится"
    mort = by_channel["спрос на ипотеку"]
    assert (mort.expected_effect, mort.because) == ("risk", ["sector=development"])
    assert effect_text(mort) == "спрос на ипотеку не восстанавливается"


def test_case_example_it():
    it = PROFILES["C-IT-01"]
    dep = next(c for c in build_chains(event_moves(event(1)), it.exposures) if c.factor == "key_rate")
    assert (dep.expected_effect, dep.company_sign, dep.because) == ("opportunity", 1, ["products.DEP"])
    assert effect_text(dep) == "доходность размещения сохраняется"


def test_opposite_sign_on_key_rate():
    effects = {}
    for company in ("C-DEV-01", "C-IT-01"):
        chains = build_chains(event_moves(event(1)), PROFILES[company].exposures)
        effects[company] = {c.expected_effect for c in chains if c.factor == "key_rate"}
    assert effects == {"C-DEV-01": {"risk"}, "C-IT-01": {"opportunity"}}


def test_chains_sorted_by_strength():
    ranks = {"risk": 0, "opportunity": 1, "mixed": 2, "neutral": 3}
    for company in PROFILES:
        for event_id in (1, 2, 3):
            chains = build_chains(event_moves(event(event_id)), PROFILES[company].exposures)
            keys = [(-c.strength, ranks[c.expected_effect]) for c in chains]
            assert keys == sorted(keys)


def test_chains_dedup_and_stable_order():
    moves = [
        move("key_rate", "flat", "high"),
        move("key_rate", "down", "low"),
        move("inflation", "up", "high"),
    ]
    exposures = [
        exposure("key_rate", strength=1, channels=["канал А", "КАНАЛ  а", "канал Б"]),
        exposure("key_rate", strength=3, channels=["канал а"]),
        exposure("inflation", sign=1, strength=2, channels=["издержки"]),
    ]
    first = build_chains(moves, exposures)
    assert first == build_chains(moves, exposures)
    keys = [(c.factor, c.channel.casefold().replace("  ", " ")) for c in first]
    assert len(keys) == len(set(keys)) == 3
    kept = next(c for c in first if c.channel.casefold().startswith("канал а"))
    assert kept.strength == 3 and kept.event_direction == "flat"


def test_no_card_gives_neutral_chains():
    ev = event(1).model_copy(update={"card": None, "factors": ["key_rate", "key_rate"]})
    moves = event_moves(ev)
    assert moves == [FactorMove("key_rate", FactorDirection.unclear, Magnitude.low, None)]
    chains = build_chains(moves, PROFILES["C-DEV-01"].exposures)
    assert chains
    assert {c.expected_effect for c in chains} == {"neutral"}
    assert {c.event_direction for c in chains} == {"unclear"}


def test_event_moves_take_first_occurrence_from_card():
    ev = event(1)
    card = ev.card.model_copy(
        update={"factors": [ev.card.factors[0], ev.card.factors[0].model_copy(update={"direction": "up"})]}
    )
    moves = event_moves(ev.model_copy(update={"card": card}))
    assert [(m.factor, m.direction) for m in moves] == [("key_rate", "flat")]
    assert moves[0].quote == ev.card.factors[0].quote


def test_exposure_without_channels():
    chains = build_chains([move()], [exposure(channels=[])])
    assert [c.channel for c in chains] == [factor_names()["key_rate"]]


def test_because_all_when_channel_unknown():
    ex = exposure(channels=["некий канал"], because=["products.LOA", "sector=x"])
    assert build_chains([move()], [ex])[0].because == ["products.LOA", "sector=x"]


def test_because_all_when_no_match_for_known_channel():
    ex = exposure(channels=["стоимость кредитов"], because=["sector=x"])
    assert build_chains([move()], [ex])[0].because == ["sector=x"]


def test_unknown_channel_generic_text():
    chain = build_chains([move(direction="up")], [exposure(channels=["некий канал"])])[0]
    assert effect_text(chain) == "Ключевая ставка ЦБ: рост — риск через «некий канал»"
    chain = build_chains([move(direction="down")], [exposure(channels=["некий канал"])])[0]
    assert effect_text(chain) == "Ключевая ставка ЦБ: снижение — возможность через «некий канал»"


def test_general_templates_for_unclear_volatile_and_zero_sign():
    name = factor_names()["key_rate"]
    zero = build_chains([move(direction="up")], [exposure(sign=0, channels=["стоимость кредитов"])])[0]
    assert effect_text(zero) == f"{name}: знак влияния не определён — стоимость кредитов"
    assert link_text(zero) == f"{name}: рост — знак влияния на компанию не определён (стоимость кредитов)."
    unclear = build_chains([move(direction="unclear")], [exposure(channels=["стоимость кредитов"])])[0]
    assert effect_text(unclear) == f"{name}: направление неясно — канал «стоимость кредитов»"
    volatile = build_chains([move(direction="volatile")], [exposure(channels=["стоимость кредитов"])])[0]
    assert effect_text(volatile) == f"{name}: колебания — стоимость кредитов труднее планировать"


def test_link_text():
    chain = build_chains([move()], [exposure(channels=["стоимость кредитов"])])[0]
    assert (
        link_text(chain)
        == "Ключевая ставка ЦБ: без изменений — влияет на компанию через «стоимость кредитов»."
    )


def test_channel_texts_have_no_digits():
    for channels in channel_texts().values():
        for entry in channels.values():
            for text in (entry.text, *entry.effects.values()):
                assert not re.search(r"\d", text), text
                assert "!" not in text


def test_channel_texts_cover_fixture_channels():
    for profile in PROFILES.values():
        for ex in profile.exposures:
            for channel in ex.channels:
                assert find_channel(ex.factor, channel) is not None, (ex.factor, channel)


def test_channel_texts_factors_in_catalog():
    assert set(channel_texts()) <= factor_ids()
    assert len(channel_texts()) == 16
    for channels in channel_texts().values():
        for entry in channels.values():
            assert set(entry.effects) == {FactorDirection.up, FactorDirection.down, FactorDirection.flat}


def test_find_channel_by_id_and_normalized_text():
    by_id = find_channel("key_rate", "debt_cost")
    assert by_id is not None and by_id is find_channel("key_rate", "Стоимость  кредитов")
    assert find_channel("key_rate", "нет такого") is None
    assert find_channel("no_such_factor", "debt_cost") is None


@pytest.mark.parametrize(
    ("because", "key"),
    [
        ("sector=development", "sector"),
        ("sector=metallurgy: экспорт", "sector"),
        ("products.LOA", "products.LOA"),
        ("suppliers.metallurgy=0.17", "suppliers.metallurgy"),
        ("sector : x", "sector"),
    ],
)
def test_feature_key(because, key):
    assert feature_key(because) == key


def test_direction_noun_covers_all_directions():
    assert {direction_noun(d) for d in FactorDirection} == {
        "рост",
        "снижение",
        "без изменений",
        "колебания",
        "направление неясно",
    }


def test_chain_channel_is_clipped():
    chains = build_chains([move()], [exposure(channels=["слово " * 100])])
    assert len(chains[0].channel) <= 200
    assert isinstance(chains[0], Chain)
