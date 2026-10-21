import base64
import json
import os
import re
import statistics
import struct
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.llm.budget import MSK
from app.llm.config import ModelsConfig
from app.llm.errors import BudgetExceeded
from app.llm.gateway import IMAGE_TOKENS, Gateway, RawAnswer, llm_scope
from app.llm.jsonmode import parse_json, strict_schema
from app.llm.provider import UNKNOWN_MODEL_RE, BadRequest, estimate_tokens

CAPS: tuple[str, ...] = ("json_schema", "json_object", "tools", "vision")
CALLS_PER_MODEL = 8  # simple ×3, reasoning_off, json_schema, json_object, tools, vision
NOTE_LIMIT = 160
# «Париж» — меньше 10 токенов. Больше 40 без текста рассуждений — провайдер их скрыл;
# до 15 — рассуждений не было; между — не знаем.
HIDDEN_REASONING_TOKENS = 40
NO_REASONING_TOKENS = 15

Status = Literal["ok", "unavailable", "error"]

SIMPLE_PROMPT = "Какой город — столица Франции? Ответь одним словом."
# без слова JSON: валидный ответ по схеме докажет, что схему применяет провайдер, а не догадка модели
SCHEMA_PROMPT = "Назови столицу Франции, страну и население города в миллионах человек."
OBJECT_PROMPT = 'Верни JSON-объект с полями "city" и "country" для столицы Франции.'
TOOLS_PROMPT = "Какая сейчас ключевая ставка в России? Узнай её через функцию."
VISION_PROMPT = (
    "Какого цвета левая и правая половины картинки? Ответь двумя словами: цвет слева, цвет справа."
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_key_rate",
            "description": "Текущая ключевая ставка центрального банка страны, % годовых",
            "parameters": {
                "type": "object",
                "properties": {"country": {"type": "string", "description": "Страна, например «Россия»"}},
                "required": ["country"],
            },
        },
    }
]
_PARIS = ("париж", "paris")
_RED = ("красн", "red")
_GREEN = ("зел", "green")


class ProbeAuthError(Exception):
    """Провайдер отверг ключ (401): дальше проверять бессмысленно."""


class _Capital(BaseModel):
    city: str
    country: str
    population_millions: float


def _png(width: int = 64, height: int = 64) -> bytes:
    # левая половина красная, правая зелёная: одну краску модель могла бы угадать, две — вряд ли
    left, right = b"\xff\x00\x00", b"\x00\xa0\x00"
    row = b"\x00" + left * (width // 2) + right * (width - width // 2)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def _user(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


def _vision_messages() -> list[dict[str, Any]]:
    url = "data:image/png;base64," + base64.b64encode(_png()).decode("ascii")
    content = [{"type": "text", "text": VISION_PROMPT}, {"type": "image_url", "image_url": {"url": url}}]
    return [{"role": "user", "content": content}]


def _schema_format() -> dict[str, Any]:
    # тот же вид, что шлюз отправляет в режиме schema
    return {
        "type": "json_schema",
        "json_schema": {"name": "Capital", "schema": strict_schema(_Capital.model_json_schema())},
    }


@dataclass
class Check:
    ok: bool | None  # None: проверить не удалось (сеть, таймаут, 5xx) — заявленное не меняется
    ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float = 0.0
    note: str = ""


@dataclass
class ModelReport:
    name: str
    uri: str
    status: Status = "ok"
    checks: dict[str, Check] = field(default_factory=dict)
    reasoning_default: bool | None = None
    reasoning_off_param: bool | None = None
    latency_ms: int | None = None
    caps: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def cost_rub(self) -> float:
        return sum(c.cost_rub for c in self.checks.values())


@dataclass
class ProbeResult:
    catalog: list[str] | None
    reports: list[ModelReport]
    # прогон остановлен (ключ отвергнут, кончился бюджет): готовые отчёты всё равно пишутся
    stopped: str | None = None


def _contains(text: str, words: Iterable[str]) -> bool:
    lower = text.lower()
    return any(w in lower for w in words)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= NOTE_LIMIT else text[: NOTE_LIMIT - 1] + "…"


def _check(answer: RawAnswer, ok: bool | None, note: str = "") -> Check:
    result = answer.result
    return Check(
        ok=ok,
        ms=answer.ms,
        tokens_in=result.tokens_in if result else 0,
        tokens_out=result.tokens_out if result else 0,
        cost_rub=answer.cost_rub,
        note=_short(note),
    )


def _failure(answer: RawAnswer) -> Check:
    """Ошибка провайдера: запрос отвергнут (4xx) — возможности нет; временный сбой — неизвестно."""
    error = answer.error
    assert error is not None
    status = error.status
    if status == 401:
        raise ProbeAuthError(f"провайдер отверг ключ: {error}")
    # 403 после удачного simple — про доступ, а не про возможность; 408 и 429 — временные
    rejected = status is not None and 400 <= status < 500 and status not in (403, 408, 429)
    if isinstance(error, BadRequest) or rejected:
        return _check(answer, False, str(error))
    return _check(answer, None, str(error))


def _unknown_model(answer: RawAnswer) -> bool:
    # 403 на первом вызове — модель не подключена каталогу: для неё это то же, что «не найдена»
    error = answer.error
    return error is not None and (
        error.status in (403, 404) or (error.status == 400 and bool(UNKNOWN_MODEL_RE.search(str(error))))
    )


def _truncated(answer: RawAnswer, check: Check) -> Check:
    """Ответ оборван по max_tokens: это сбой прогона, а не отсутствие возможности."""
    if check.ok is False and answer.result is not None and answer.result.finish_reason == "length":
        check.ok = None
        check.note = _short(f"ответ оборван по max_tokens: {check.note}")
    return check


def _reasons(answer: RawAnswer) -> bool | None:
    result = answer.result
    assert result is not None
    if result.reasoning or (not result.text.strip() and result.finish_reason == "length"):
        return True
    if result.tokens_out > HIDDEN_REASONING_TOKENS:
        return True
    if result.tokens_out <= NO_REASONING_TOKENS:
        return False
    return None


def _judge_simple(answer: RawAnswer) -> Check:
    if answer.error is not None:
        return _failure(answer)
    assert answer.result is not None
    text = answer.result.text.strip()
    if _contains(text, _PARIS):
        return _check(answer, True)
    if not text and answer.result.finish_reason == "length":
        return _check(answer, False, "пустой ответ: рассуждения заняли весь max_tokens")
    return _check(answer, False, f"ответ: {text}")


def _judge_schema(answer: RawAnswer) -> Check:
    if answer.error is not None:
        return _failure(answer)
    assert answer.result is not None
    text = answer.result.text
    try:
        data = _Capital.model_validate(parse_json(text))
    except (ValueError, ValidationError) as exc:
        return _check(answer, False, f"ответ не по схеме ({type(exc).__name__}): {text}")
    if not _contains(data.city, _PARIS):
        return _check(answer, False, f"схема соблюдена, но город {data.city!r}")
    return _check(answer, True, "в ответе обёртка ```" if "```" in text else "")


def _judge_object(answer: RawAnswer) -> Check:
    if answer.error is not None:
        return _failure(answer)
    assert answer.result is not None
    text = answer.result.text
    try:
        data = parse_json(text)
    except ValueError:
        return _check(answer, False, f"не JSON: {text}")
    if not isinstance(data, dict) or "city" not in data:
        return _check(answer, False, f"нет поля city: {text}")
    return _check(answer, True, "в ответе обёртка ``` — шлюз её снимает" if "```" in text else "")


def _judge_tools(answer: RawAnswer) -> Check:
    if answer.error is not None:
        return _failure(answer)
    assert answer.result is not None
    calls = answer.result.tool_calls
    if not calls:
        return _check(answer, False, f"функцию не вызвала, ответила текстом: {answer.result.text}")
    call = calls[0]
    if call.name != "get_key_rate" or not str(call.args.get("country") or "").strip():
        return _check(answer, False, f"вызов {call.name}({json.dumps(call.args, ensure_ascii=False)})")
    return _check(answer, True)


def _judge_vision(answer: RawAnswer) -> Check:
    if answer.error is not None:
        return _failure(answer)
    assert answer.result is not None
    text = answer.result.text
    if _contains(text, _RED) and _contains(text, _GREEN):
        return _check(answer, True)
    return _check(answer, False, f"цвета не названы: {text}")


def final_caps(declared: Iterable[str], checks: dict[str, Check]) -> list[str]:
    caps = set(declared)
    for cap in CAPS:
        check = checks.get(cap)
        if check is None or check.ok is None:
            continue
        if check.ok:
            caps.add(cap)
        else:
            caps.discard(cap)
    return sorted(caps)


async def probe_model(gw: Gateway, name: str) -> ModelReport:
    mcfg = gw.config.models[name]
    report = ModelReport(name=name, uri=mcfg.uri, caps=sorted(mcfg.caps))
    first = await gw.raw_call(name, _user(SIMPLE_PROMPT))
    if _unknown_model(first):
        report.status, report.note = "unavailable", _short(str(first.error))
        return report
    simple = _judge_simple(first)
    report.checks["simple"] = simple
    if first.result is None:
        report.status, report.note = "error", simple.note
        return report
    report.reasoning_default = _reasons(first)

    off = await gw.raw_call(name, _user(SIMPLE_PROMPT), reasoning_off=True)
    report.checks["reasoning_off"] = _judge_simple(off)
    if off.result is not None:
        # параметр принят, но рассуждений не погасил — для шлюза это то же, что не принят
        report.reasoning_off_param = _reasons(off) is not True
        if not report.reasoning_off_param:
            check = report.checks["reasoning_off"]
            check.note = _short(f"параметр принят, но модель рассуждала. {check.note}")
    elif isinstance(off.error, BadRequest):
        report.reasoning_off_param = False

    # дальше — режим, в котором модель будет звать шлюз; не знаем, рассуждает ли, — как в конфиге
    quiet = bool(report.reasoning_off_param) and (
        report.reasoning_default is True
        or (report.reasoning_default is None and mcfg.reasoning_off == "reasoning_effort")
    )
    runs = [off if quiet else first]
    for _ in range(2):
        runs.append(await gw.raw_call(name, _user(SIMPLE_PROMPT), reasoning_off=quiet))
    for i, extra in enumerate(runs[1:], start=2):
        report.checks[f"latency_{i}"] = _judge_simple(extra)
    times = [r.ms for r in runs if r.result is not None]
    report.latency_ms = int(statistics.median(times)) if times else None

    checks: list[tuple[str, Callable[[RawAnswer], Check], list[dict[str, Any]], dict[str, Any]]] = [
        ("json_schema", _judge_schema, _user(SCHEMA_PROMPT), {"response_format": _schema_format()}),
        ("json_object", _judge_object, _user(OBJECT_PROMPT), {"response_format": {"type": "json_object"}}),
        ("tools", _judge_tools, _user(TOOLS_PROMPT), {"tools": TOOLS}),
        ("vision", _judge_vision, _vision_messages(), {}),
    ]
    for cap, judge, messages, extra in checks:
        answer = await gw.raw_call(name, messages, reasoning_off=quiet, **extra)
        report.checks[cap] = _truncated(answer, judge(answer))
    report.caps = final_caps(mcfg.caps, report.checks)
    return report


async def run_probe(
    gw: Gateway, models: list[str], *, on_model: Callable[[ModelReport], None] | None = None
) -> ProbeResult:
    providers = {gw.config.models[m].provider for m in models}
    catalog: list[str] | None = None
    for name in sorted(providers):
        provider = await gw.provider(name)
        listed = await provider.list_models()
        if listed is not None:
            catalog = [*(catalog or []), *(provider.mask(m) for m in listed)]
    result = ProbeResult(catalog, [])
    # один trace_id на прогон: его стоимость — один запрос к llm_calls
    with llm_scope(trace_id="probe-" + datetime.now(MSK).strftime("%Y%m%d-%H%M%S")):
        # по очереди: параллельные вызовы исказили бы задержку
        for name in models:
            try:
                report = await probe_model(gw, name)
            except (ProbeAuthError, BudgetExceeded) as exc:
                result.stopped = str(exc)
                break
            result.reports.append(report)
            if on_model is not None:
                on_model(report)
    return result


def _model_name(uri_or_id: str) -> str:
    # gpt://<каталог>/<имя>/latest → <имя>
    tail = uri_or_id.split("://", 1)[-1]
    parts = [p for p in tail.split("/") if p]
    if "://" in uri_or_id and len(parts) > 1:
        parts = parts[1:]
    if len(parts) > 1 and parts[-1] in ("latest", "rc", "deprecated"):
        parts = parts[:-1]
    return parts[-1].lower() if parts else ""


def _family(name: str) -> str:
    match = re.match(r"[a-z]+", name.lower())
    return match.group(0) if match else name.lower()


def catalog_matches(config: ModelsConfig, catalog: list[str]) -> dict[str, tuple[bool, list[str]]]:
    """Для каждой модели конфига: есть ли её URI в каталоге и какие модели того же семейства там есть."""
    names = {entry: _model_name(entry) for entry in catalog}
    out = {}
    for model, mcfg in config.models.items():
        wanted = _model_name(mcfg.uri)
        found = any(n == wanted for n in names.values())
        family = sorted({n for n in names.values() if _family(n) == _family(wanted)})
        out[model] = (found, family)
    return out


def estimate(config: ModelsConfig, models: list[str]) -> list[tuple[str, int, float]]:
    """Смета сверху: вход — по тексту запросов, выход — max_tokens задачи probe на каждый вызов."""
    max_out = config.task("probe").max_tokens
    texts = [SIMPLE_PROMPT] * 4 + [
        SCHEMA_PROMPT + json.dumps(_schema_format(), ensure_ascii=False),
        OBJECT_PROMPT,
        TOOLS_PROMPT + json.dumps(TOOLS, ensure_ascii=False),
    ]
    tokens_in = sum(estimate_tokens(t) for t in texts) + estimate_tokens(VISION_PROMPT) + IMAGE_TOKENS
    tokens_out = max_out * CALLS_PER_MODEL
    rows = []
    for name in models:
        price = config.models[name].price
        rows.append((name, CALLS_PER_MODEL, tokens_in * price.in_ / 1000 + tokens_out * price.out / 1000))
    return rows


def _report_json(report: ModelReport, probed_at: str) -> dict[str, Any]:
    return {
        "probed_at": probed_at,
        "uri": report.uri,
        "status": report.status,
        "caps": report.caps,
        "reasoning_default": report.reasoning_default,
        "reasoning_off_param": report.reasoning_off_param,
        "latency_ms": report.latency_ms,
        "note": report.note,
        "checks": {
            name: {
                "ok": c.ok,
                "ms": c.ms,
                "tokens_in": c.tokens_in,
                "tokens_out": c.tokens_out,
                "cost_rub": round(c.cost_rub, 4),
                "note": c.note,
            }
            for name, c in report.checks.items()
        },
    }


def write_caps(path: Path, result: ProbeResult, now: datetime | None = None) -> None:
    """Пишет итог; записи моделей, которые в этот раз не проверялись, сохраняются."""
    models: dict[str, Any] = {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(previous, dict) and isinstance(previous.get("models"), dict):
            models = previous["models"]
    except (OSError, ValueError):
        pass
    probed_at = (now or datetime.now(MSK)).isoformat(timespec="seconds")
    for report in result.reports:
        models[report.name] = _report_json(report, probed_at)
    data: dict[str, Any] = {"probed_at": probed_at, "models": dict(sorted(models.items()))}
    if result.catalog is not None:
        data["catalog"] = sorted(result.catalog)
    # через временный файл: шлюз в соседнем процессе не должен прочитать половину файла
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def discrepancies(config: ModelsConfig, result: ProbeResult) -> list[str]:
    """Что поправить в models.yaml по итогам прогона: конфиг правит человек, через ревью."""
    lines = []
    matches = catalog_matches(config, result.catalog) if result.catalog else {}
    for report in result.reports:
        mcfg = config.models[report.name]
        if report.status == "unavailable":
            found, family = matches.get(report.name, (False, []))
            hint = f"; у провайдера из семейства: {', '.join(family)}" if family else ""
            lines.append(f"{report.name}: URI {mcfg.uri} не найден у провайдера{hint}")
            continue
        if report.status == "error":
            lines.append(f"{report.name}: проверить не удалось — {report.note}")
            continue
        declared = set(mcfg.caps)
        added = sorted(set(report.caps) - declared)
        removed = sorted(declared - set(report.caps))
        if added:
            lines.append(f"{report.name}: добавить в caps {', '.join(added)}")
        if removed:
            lines.append(f"{report.name}: убрать из caps {', '.join(removed)}")
        unknown = [cap for cap in CAPS if (c := report.checks.get(cap)) is not None and c.ok is None]
        if unknown:
            lines.append(f"{report.name}: не проверено (сбой) — {', '.join(unknown)}; повторить --model")
        if report.reasoning_default is None:
            lines.append(
                f"{report.name}: рассуждает ли по умолчанию — не ясно, смотреть токены simple в файле"
            )
        quiet = report.reasoning_default and report.reasoning_off_param
        if quiet and mcfg.reasoning_off != "reasoning_effort":
            lines.append(f"{report.name}: рассуждает по умолчанию — reasoning_off: reasoning_effort")
        if report.reasoning_default and report.reasoning_off_param is False:
            lines.append(f"{report.name}: рассуждения не отключаются параметром — учесть в max_tokens задач")
        if report.reasoning_default is False and mcfg.reasoning_off == "reasoning_effort":
            lines.append(f"{report.name}: без рассуждений по умолчанию — reasoning_off можно убрать")
    chart = config.tasks.get("chart_read")
    by_name = {r.name: r for r in result.reports}
    if chart is not None and chart.model in by_name and "vision" not in by_name[chart.model].caps:
        seeing = [r.name for r in result.reports if "vision" in r.caps and r.name != chart.model]
        hint = f"; картинки понимают: {', '.join(seeing)}" if seeing else ""
        lines.append(f"chart_read: у {chart.model} нет vision{hint}")
    return lines


def _mark(check: Check | None) -> str:
    if check is None:
        return "—"
    return {True: "да", False: "нет", None: "?"}[check.ok]


def render_table(reports: list[ModelReport]) -> list[str]:
    head = f"{'модель':<28}{'статус':<13}{'schema':<8}{'object':<8}{'tools':<7}{'vision':<8}"
    head += f"{'рассужд.':<10}{'мс':>7}{'₽':>8}"
    lines = [head]
    for r in reports:
        reasoning = {True: "да", False: "нет", None: "?"}[r.reasoning_default]
        if r.reasoning_default and r.reasoning_off_param:
            reasoning = "откл."
        latency = str(r.latency_ms) if r.latency_ms is not None else "—"
        lines.append(
            f"{r.name:<28}{r.status:<13}"
            f"{_mark(r.checks.get('json_schema')):<8}{_mark(r.checks.get('json_object')):<8}"
            f"{_mark(r.checks.get('tools')):<7}{_mark(r.checks.get('vision')):<8}"
            f"{reasoning:<10}{latency:>7}{r.cost_rub:>8.3f}"
        )
    return lines
