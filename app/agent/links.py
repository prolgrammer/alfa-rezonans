import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.catalogs import factor_ids, factor_names
from app.contracts import (
    Chain,
    Event,
    Exposure,
    FactorDirection,
    ImpactDirection,
    Magnitude,
)
from app.text.verify import clip, normalize

CHANNEL_LIMIT = 200

_MAGNITUDE_WEIGHT = {Magnitude.low: 1, Magnitude.medium: 2, Magnitude.high: 3}
# при равной силе опасное идёт раньше полезного, определённое — раньше неопределённого
_EFFECT_RANK = {
    ImpactDirection.risk: 0,
    ImpactDirection.opportunity: 1,
    ImpactDirection.mixed: 2,
    ImpactDirection.neutral: 3,
}
_DIRECTION_NOUN = {
    FactorDirection.up: "рост",
    FactorDirection.down: "снижение",
    FactorDirection.flat: "без изменений",
    FactorDirection.volatile: "колебания",
    FactorDirection.unclear: "направление неясно",
}
_FEATURE_KEY_SPLIT = re.compile(r"[=:]")


@dataclass(frozen=True)
class FactorMove:
    factor: str
    direction: FactorDirection
    magnitude: Magnitude
    quote: str | None


@dataclass(frozen=True)
class ChannelText:
    id: str
    text: str
    when: str | None
    effects: Mapping[FactorDirection, str]


class _ChannelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    when: str | None = None
    up: str = Field(min_length=1)
    down: str = Field(min_length=1)
    flat: str = Field(min_length=1)


_CHANNELS_FILE = TypeAdapter(dict[str, dict[str, _ChannelEntry]])


def event_moves(event: Event) -> list[FactorMove]:
    if event.card is not None:
        found = [FactorMove(f.id, f.direction, f.magnitude, f.quote) for f in event.card.factors]
    else:
        # без карточки направление неизвестно, и такие цепочки ничего не утверждают о знаке
        found = [FactorMove(f, FactorDirection.unclear, Magnitude.low, None) for f in event.factors]
    moves: dict[str, FactorMove] = {}
    for move in found:
        moves.setdefault(move.factor, move)
    return list(moves.values())


def expected_effect(direction: FactorDirection, sign: int) -> ImpactDirection:
    if sign == 0 or direction == FactorDirection.unclear:
        return ImpactDirection.neutral
    if direction == FactorDirection.volatile:
        return ImpactDirection.mixed
    # flat читается как «уровень фактора сохраняется»: давление или выгода остаются, как при росте
    gain_on_rise = sign > 0
    if direction == FactorDirection.down:
        gain_on_rise = not gain_on_rise
    return ImpactDirection.opportunity if gain_on_rise else ImpactDirection.risk


def chain_strength(exposure_strength: int, magnitude: Magnitude) -> int:
    product = exposure_strength * _MAGNITUDE_WEIGHT[magnitude]
    if product <= 2:
        return 1
    return 2 if product <= 4 else 3


# заглушка до DQ-6 роли 2
@lru_cache
def channel_texts() -> Mapping[str, Mapping[str, ChannelText]]:
    raw = yaml.safe_load(Path(__file__).with_name("channels.yaml").read_text(encoding="utf-8"))
    parsed = _CHANNELS_FILE.validate_python(raw)
    unknown = sorted(set(parsed) - factor_ids())
    if unknown:
        raise ValueError(f"в channels.yaml факторы вне каталога: {', '.join(unknown)}")
    result: dict[str, dict[str, ChannelText]] = {}
    for factor, channels in parsed.items():
        seen: set[str] = set()
        result[factor] = {}
        for channel_id, entry in channels.items():
            key = normalize(entry.text)
            if key in seen:
                raise ValueError(f"в channels.yaml у фактора {factor} повторяется канал «{entry.text}»")
            seen.add(key)
            result[factor][channel_id] = ChannelText(
                id=channel_id,
                text=entry.text,
                when=entry.when,
                effects={
                    FactorDirection.up: entry.up,
                    FactorDirection.down: entry.down,
                    FactorDirection.flat: entry.flat,
                },
            )
    return result


def find_channel(factor: str, channel: str) -> ChannelText | None:
    wanted = normalize(channel)
    for entry in channel_texts().get(factor, {}).values():
        if channel == entry.id or wanted == normalize(entry.text):
            return entry
    return None


def feature_key(because: str) -> str:
    return _FEATURE_KEY_SPLIT.split(because, maxsplit=1)[0].replace(" ", "")


def direction_noun(direction: FactorDirection) -> str:
    return _DIRECTION_NOUN[direction]


def _because(exposure: Exposure, channel: str) -> list[str]:
    entry = find_channel(exposure.factor, channel)
    if entry is not None and entry.when is not None:
        matched = [b for b in exposure.because if feature_key(b) == entry.when]
        if matched:
            return matched
    return list(exposure.because)


def build_chains(moves: Sequence[FactorMove], exposures: Sequence[Exposure]) -> list[Chain]:
    chains: list[Chain] = []
    position: dict[tuple[str, str], int] = {}
    for move in moves:
        for exposure in exposures:
            if exposure.factor != move.factor:
                continue
            fallback = factor_names().get(exposure.factor, exposure.factor)
            for channel in [c for c in exposure.channels if c.strip()] or [fallback]:
                chain = Chain(
                    factor=move.factor,
                    event_direction=move.direction,
                    company_sign=exposure.sign,
                    expected_effect=expected_effect(move.direction, exposure.sign),
                    channel=clip(channel, CHANNEL_LIMIT),
                    strength=chain_strength(exposure.strength, move.magnitude),
                    because=_because(exposure, channel),
                )
                key = (move.factor, normalize(chain.channel))
                if key not in position:
                    position[key] = len(chains)
                    chains.append(chain)
                elif chain.strength > chains[position[key]].strength:
                    chains[position[key]] = chain
    return sorted(chains, key=lambda c: (-c.strength, _EFFECT_RANK[c.expected_effect]))


def effect_text(chain: Chain) -> str:
    name = factor_names().get(chain.factor, chain.factor)
    if chain.company_sign == 0:
        return f"{name}: знак влияния не определён — {chain.channel}"
    if chain.event_direction == FactorDirection.unclear:
        return f"{name}: направление неясно — канал «{chain.channel}»"
    if chain.event_direction == FactorDirection.volatile:
        return f"{name}: колебания — {chain.channel} труднее планировать"
    entry = find_channel(chain.factor, chain.channel)
    if entry is not None:
        return entry.effects[chain.event_direction]
    kind = "риск" if chain.expected_effect == ImpactDirection.risk else "возможность"
    return f"{name}: {direction_noun(chain.event_direction)} — {kind} через «{chain.channel}»"


def link_text(chain: Chain) -> str:
    name = factor_names().get(chain.factor, chain.factor)
    noun = direction_noun(chain.event_direction)
    if chain.company_sign == 0:
        return f"{name}: {noun} — знак влияния на компанию не определён ({chain.channel})."
    return f"{name}: {noun} — влияет на компанию через «{chain.channel}»."
