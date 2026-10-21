# Фундамент репозитория: общие решения

Дата: 2026-10-20. Статус: принято, меняется только PR с одобрением затронутых ролей.

Цель — чтобы после слияния фундамента каждая из четырёх ролей писала свой код против замороженных форм данных и заглушек соседей и никого не ждала. Ниже — решения по всему, что иначе пришлось бы согласовывать по ходу: конвенции, справочники, модели, схема БД, сигнатуры на границах ролей, API, фикстуры. Источники требований — общее ТЗ (р. 3–8, 10, 14) и ТЗ ролей; здесь они не пересказываются, а доопределяются там, где ТЗ оставляло выбор.

## 1. Конвенции

| Вопрос | Решение | Почему |
| --- | --- | --- |
| Идентификаторы | Внутренние сущности — `BIGINT GENERATED ALWAYS AS IDENTITY`. Компания — `TEXT`, значение `SYNTHETIC_U` из CSV как есть. Справочники (факторы, отрасли, источники, роли) — `TEXT`-слаги латиницей. Внутри разбора — локальные строки: утверждения `s1…`, доказательства `E1…` | Сквозные id компаний совпадают с данными организаторов; слаги читаются в YAML и логах; bigint безопасен для JS до 2^53 |
| Время | В БД — `TIMESTAMPTZ`, хранится UTC. API отдаёт ISO 8601 со смещением. Интерфейс показывает МСК (`Europe/Moscow`). Календарные даты без времени — `DATE`, ISO | Одна точка правды, без двусмысленности при склейке и свежести |
| Деньги | Стоимость вызовов LLM — `NUMERIC(12,4)` ₽. Выручка и платежи — `NUMERIC(20,2)` в единицах CSV (единицы уточняем у организаторов). В API — число | Копейки на сотнях вызовов в сутки иначе теряются |
| Значения перечислений | Латиница `snake_case` в коде, БД и API; русские подписи — только в конфигах и интерфейсе | Стабильно для типов TS и тестов |
| Имена полей | `snake_case` везде, включая API и сгенерированные типы TS (без алиасов в camelCase) | Один и тот же JSON в фикстурах, БД (JSONB) и на фронте |
| Роли людей | `user_role` — роль в компании: `owner`, `ceo`, `cfo`, `treasurer`, `chief_accountant`, `other`. `access` — права в сервисе: `client`, `expert`, `admin` | Слово «роль» иначе значит три разных вещи (ещё и роль в команде) |
| Ошибки API | Тело `{"error": {"code": "<slug>", "message": "<по-русски для человека>", "details": {…}}}`; коды: 400 `invalid_request`, 401 `unauthorized`, 403 `forbidden`, 404 `not_found` (в том числе чужая компания; в коде — только исключение `NotFound` из `app/errors.py`, остальные `LookupError`/`KeyError` дают 500), 409 `conflict`, 422 `validation_error`, 429 `rate_limited`, 503 `llm_unavailable`, 503 `not_implemented` (маршрут ещё на заглушке вне режима MOCK), 500 `internal_error` (необработанная ошибка; причина только в журнале сервера, в кадре SSE `error` — тот же код) | Интерфейс показывает `message`, логика ветвится по `code` |
| Модели pydantic | `ConfigDict(extra="forbid")` у всего контракта; ограничения длины и диапазонов — в полях (`Field(max_length=…)`, `ge`/`le`) | Ответ модели с лишним или сломанным полем падает на валидации, а не уходит дальше |
| Асинхронность | Всё, что на границе ролей ходит в БД, сеть или LLM, — `async def`. Чистые вычисления над переданными данными — обычные функции | FastAPI и worker асинхронные; расчёты профиля и формулы — numpy без ввода-вывода |
| Контекст пользователя | Объект `SessionCtx` (`user_id`, `company_id`, `user_role`, `access`) передаётся явно во все функции, которым нужен клиент. Из запроса компания не берётся никогда | Изоляция клиентов (р. 10) проверяется по сигнатурам |
| Язык | Код и идентификаторы — английский; комментарии, тексты, коммиты, PR, документы — русский | Правила ТЗ, р. 14 |

## 2. Справочники id

Фундамент фиксирует только id и русские названия. Наполнение (леммы, каналы, ряды, сопоставление ОКВЭД, матрица) — работа роли 2; добавить id можно своим PR, переименовать или удалить — только вместе.

**Факторы** (`config/factors.yaml`, 36 записей, группы — как в общем ТЗ, р. 4, A3):

- денежно-кредитные: `key_rate`, `inflation`, `ofz_yields`, `credit_availability`;
- валюта: `usd_rub`, `eur_rub`, `cny_rub`, `fx_control`;
- сырьё: `oil`, `gas`, `coal`, `steel`, `aluminum`, `copper`, `nickel`, `gold`, `grain`, `fertilizers`, `timber`;
- спрос: `consumer_demand`, `investment_demand`, `gdp_growth`, `mortgage_housing`, `public_procurement`;
- издержки: `wages_labor`, `energy_tariffs`, `fuel`, `logistics_freight`;
- регулирование: `taxes`, `export_duties`, `import_duties`, `labeling`, `sector_regulation`;
- внешние: `sanctions`, `cross_border_payments`, `china_trade`.

**Отрасли** (`config/okved_sectors.yaml`, раздел `sectors`, 35 записей): `agriculture`, `food`, `coal_mining`, `oil_gas`, `metal_ore_mining`, `metallurgy`, `chemicals`, `fertilizers_prod`, `pharma`, `timber_paper`, `construction_materials`, `machinery`, `automotive`, `electronics_electrical`, `light_industry`, `power`, `utilities`, `development`, `construction`, `auto_trade`, `wholesale`, `retail`, `transport_logistics`, `hospitality`, `media`, `telecom`, `it`, `finance`, `real_estate`, `professional_services`, `public_admin`, `education`, `healthcare`, `culture_sport`, `other_services`.

**Прочие закрытые списки** (`app/contracts/enums.py`):

| Перечисление | Значения |
| --- | --- |
| `EventType` | `regulator_decision`, `macro_stats`, `prices_markets`, `currency`, `taxes_law`, `sanctions_trade`, `sector`, `corporate`, `infrastructure_logistics`, `emergency`, `other` |
| `Certainty` | `happened`, `decided`, `planned`, `rumor` |
| `Horizon` | `days`, `weeks`, `months`, `years` |
| `FactorDirection` (движение фактора в событии) | `up`, `down`, `flat`, `volatile`, `unclear` |
| `Magnitude` | `low`, `medium`, `high` |
| `SectorRole` (роль отрасли в событии) | `affected`, `beneficiary`, `source` |
| `ImpactDirection` | `risk`, `opportunity`, `mixed`, `neutral` |
| `Origin` | `fact`, `bank`, `model` |
| `Confidence` | `high`, `medium`, `low` |
| `EvidenceType` | `news`, `bank`, `market`, `feature` |
| `ThesisKind` | `forecast`, `assessment`, `fact`, `recommendation` |
| `FeatureSource` | `csv`, `calc`, `group`, `user` (в признаке — строка вида `csv:REVENUE_COMPANY`, `calc:suppliers`) |
| `SourceKind` | `official`, `news`, `industry` |
| `UserRole`, `Access` | см. р. 1 |
| `StepKind` (шаг агента) | `links`, `plan`, `tool`, `draft`, `check`, `critic`, `revise`, `final`, `fallback` |
| `FeedbackVerdict` / причина | `useful` / `miss`; `not_my_topic`, `already_knew`, `factual_error`, `wrong_conclusion` |

## 3. Модели контракта

Пакет `app/contracts/` — единственное место, где живут общие формы данных. Владельцы модулей правят их своим PR с одобрением потребителей.

| Модуль | Модели | Владелец |
| --- | --- | --- |
| `enums.py` | все перечисления р. 2 | все |
| `session.py` | `SessionCtx` | роль 3 |
| `events.py` | `EventCard` и вложенные (`CardFact`, `CardClaim`, `CardDate`, `CardNumber`, `CardCause`, `CardFactor`, `CardSector`), `NewsFilterResult` | роль 1 |
| `bank.py` | `BankThesis`, `ThesisItem` (тезис + id, документ, дата, отметка вычитки), `BankChunk` | роль 1 (тезис), роль 3 (фрагмент) |
| `insight.py` | `Evidence`, `Statement`, `Link`, `Impact`, `Scenario`, `BankView`, `Offer`, `Insight`, `InsightChecks`, `InsightResult`, `Chain` (цепочка D2) | роль 1 |
| `trace.py` | `AgentStep`, `ToolCallRecord` | роль 1 |
| `profile.py` | `Feature`, `CompanyProfile`, `Exposure`, `CompactProfile`, `CounterpartyShare` | роль 2 |
| `rank.py` | `RelevanceScore` (слагаемые, итог, стадия, причина), `WhyChip`, `FeedItem` | роль 2 |
| `market.py` | `MarketPoint`, `MarketSeries` (значение, изменения за день, неделю, месяц) | роль 3 |
| `news.py` | `RawItemIn` (публикация на входе фильтра), `NewsHit`, `SourceFragment` | роль 3 |
| дополнительно | `ChatAnswer` (в `insight.py`), `JudgeScore` (в `rank.py`) | роли 1 и 2 |

Формы `EventCard`, `BankThesis`, `Insight` и `Statement` — ровно как в общем ТЗ (р. 5, B4, B6; р. 7, D5) и ТЗ роли 1 (ML-5, ML-6, ML-11); `Exposure` — как в A4; `Feature` — `{key, value, source, detail}` из A2.

`Insight` — то, что пишет модель (схема `submit_insight`), поэтому в нём нет того, что знает только код. Готовый разбор — конверт `InsightResult`: `insight`, `evidence` (все доказательства, на которые ссылаются утверждения, — по ним интерфейс открывает источник сноски), `mode` (`full` / `brief` — краткий разбор без LLM, D9) и `checks` (`InsightChecks`: сколько утверждений проверено, сколько удалено, сколько было доработок — строка «На чём основано»). Поток разбора отдаёт `AgentStep | InsightResult`; у `AgentStep` есть `kind`, `title` (текст прогресса для интерфейса: «Смотрю позицию банка…»), краткие вход и выход, `model`, `tokens_in`, `tokens_out`, `cost_rub`, `ms`.

## 4. Схема БД `0001_init`

PostgreSQL 16 + pgvector. Миграции — пары `migrations/NNNN_<имя>.up.sql` и `.down.sql`; свой раннер в `app/db.py` с таблицей `schema_migrations`, команда `orientir migrate [up|down N]`, тест «накатить → откатить → накатить». Свой раннер, а не отдельный образ: одна и та же команда работает в compose, в CI и в тестах.

Правила схемы: у каждой таблицы один писатель (комментарий над таблицей); полезная нагрузка, которая повторяет модель контракта, хранится JSONB целиком (`card`, `payload`, `features`), а то, по чему фильтруем и сортируем, — отдельными колонками; полнотекстовые индексы — `tsvector('russian')`; вектор `vector(384)` — колонка есть, заполняется только при `EMBED_ENABLED=1`.

| Таблица | Ключевые колонки | Пишет |
| --- | --- | --- |
| `companies` | `id TEXT PK` (`SYNTHETIC_U`), поля CSV в нижнем регистре, `group_id`, `raw JSONB` | роль 2 (`load-clients`) |
| `company_persons` | `company_id`, `person_id`, поля CSV | роль 2 |
| `payments` | `payer_id`, `beneficiary_id`, `pay_amt`, `pay_cnt`, `intragroup BOOL`; PK (`payer_id`, `beneficiary_id`) | роль 2 |
| `company_profiles` | `company_id PK`, `version`, `hash`, `completeness`, `features JSONB`, `exposures JSONB`, `computed_at` | роль 2 |
| `group_profiles` | `group_id PK`, те же колонки | роль 2 |
| `profile_overrides` | `id`, `company_id`, `user_id`, `key`, `value JSONB`, `origin` (`edit` / `text`), `created_at` | api |
| `users` | `id`, `login UNIQUE`, `password_hash`, `company_id NULL`, `user_role`, `access`, `interests JSONB`, `failed_logins`, `locked_until` | api (`seed`) |
| `sessions` | `token_hash PK`, `user_id`, `csrf_token`, `acting_company_id`, `acting_user_role` (переключение клиента экспертом), `expires_at` | api |
| `sources` | `id TEXT PK`, `name`, `type`, `kind`, `url`, `trust`, `lang`, `interval_min`, `enabled`, `disabled_reason`, `config JSONB` | worker |
| `raw_items` | `id`, `source_id`, `url`, `canonical_url UNIQUE`, `title`, `summary`, `content`, `published_at`, `fetched_at`, `content_hash`, `status` (`new` / `filtered_out` / `clustered`), `filter_reason`, `factors TEXT[]`, `sectors TEXT[]`, `regions TEXT[]`, `tsv` | worker |
| `events` | `id`, `title`, `primary_item_id`, `first_seen_at`, `last_item_at`, `source_count`, `has_official`, `factors TEXT[]`, `sectors TEXT[]`, `regions TEXT[]`, `card JSONB NULL`, `card_status` (`none` / `pending` / `ok` / `failed`), `card_key`, `conflicts JSONB`, `embedding vector(384) NULL` | worker; `card*` — функция роли 1 |
| `event_items` | `event_id`, `raw_item_id`, `is_primary`; PK пара | worker |
| `market_series` | `series`, `date`, `value`, `source`; PK (`series`, `date`) | worker |
| `bank_docs` | `id`, `title`, `published_on`, `topic`, `file_sha256 UNIQUE`, `pages`, `factors TEXT[]` | `ingest-bank` |
| `bank_chunks` | `id`, `doc_id`, `page`, `ord`, `text`, `tsv`, `embedding NULL`, `chart_png TEXT NULL` | `ingest-bank` |
| `bank_theses` | `id`, `doc_id`, `page`, `payload JSONB` (`BankThesis`), `factors TEXT[]`, `kind`, `published_on`, `verified BOOL NULL`, `prompt_version` | роль 1 |
| `relevance` | `company_id`, `user_role`, `event_id`, `score`, `terms JSONB`, `stage`, `reason`, `profile_hash`, `computed_at`; PK тройка | роль 2 |
| `insights` | `id`, `event_id`, `company_id`, `user_role`, `cache_key UNIQUE`, `status` (`pending` / `running` / `done` / `failed`), `mode` (`full` / `brief`), `payload JSONB NULL`, `cost_rub`, `created_at`, `finished_at` | роль 1 через обёртку api |
| `agent_traces` | `id`, `insight_id NULL`, `chat_id NULL`, `seq`, `step JSONB` (`AgentStep`), `created_at` | роль 1 |
| `tool_calls` | `id`, `insight_id NULL`, `chat_id NULL`, `user_id`, `tool`, `args JSONB`, `result_chars`, `duration_ms`, `ok`, `created_at` | роль 1 |
| `llm_calls` | `id`, `task`, `model`, `prompt_version`, `tokens_in`, `tokens_out`, `cost_rub`, `ms`, `cached`, `fallback_from`, `error`, `lane`, `trace_id`, `created_at` | шлюз роли 1 |
| `llm_cache` | `key TEXT PK` (sha256), `task`, `model`, `response JSONB`, `created_at` | шлюз роли 1 |
| `feedback` | `id`, `user_id`, `event_id`, `insight_id NULL`, `verdict`, `reason NULL`, `created_at` | api |
| `expert_reviews` | `id`, `insight_id`, `reviewer_id`, `action` (`accept` / `edit` / `reject`), `patch JSONB`, `comment`, `created_at` | api |
| `chat_messages` | `id`, `chat_id`, `user_id`, `event_id NULL`, `author` (`user` / `assistant`), `content`, `payload JSONB`, `created_at` | api |
| `audit_log` | `id`, `user_id`, `action`, `details JSONB`, `created_at` | api |

Индексы: `tsv` (GIN) у `raw_items` и `bank_chunks`; `published_at`, `last_item_at`; GIN по `factors` и `sectors`; `company_id` везде, где он есть; уникальность `insights.cache_key` и `llm_cache.key`. HNSW по векторам — отдельной миграцией, если разрешат эмбеддинги.

## 5. Функции на границах ролей

Каждая функция лежит по своему постоянному пути. Фундамент создаёт её с настоящей сигнатурой и телом-заглушкой, которое отдаёт данные из фикстур (или честно сообщает о недоступности, как шлюз). Владелец потом заменяет тело на месте — потребителям ничего менять не нужно. Заглушка помечена одной строкой комментария «заглушка до <пункт ТЗ>».

| Путь | Сигнатура | Владелец | Заглушка |
| --- | --- | --- | --- |
| `app/llm/gateway.py` | `async gw.json(task, prompt, vars, schema, *, event_id=None, lane="background") -> BaseModel`; `gw.tools_step(...)`, `gw.vision(...)` | 1 | бросает `LLMUnavailable` — потребители сразу пишут путь без модели |
| `app/news/extract.py` | `async news_filter(items: list[RawItemIn]) -> list[NewsFilterResult]`; `async extract_event_card(event_id: int) -> EventCard \| None` | 1 | `business=True`; карточка из фикстур по id, иначе `None` |
| `app/bank/theses.py` | `async extract_theses(doc_id: int) -> list[BankThesis]`; `async get_bank_view(factor: str, limit: int = 3) -> list[ThesisItem]` (свежие проверенные тезисы для инструмента `get_bank_view` и строки «Банк: …» ленты) | 1 | тезисы из фикстур |
| `app/rank/judge.py` | `async rank_judge(profile: CompactProfile, cards: list[tuple[int, EventCard]]) -> list[JudgeScore]` (пара «id события, карточка»: в самой карточке id нет) | 1 | пустой список (смешивание без судьи) |
| `app/agent/run.py` | `run_insight(ctx: SessionCtx, event_id: int) -> AsyncIterator[AgentStep \| InsightResult]`; `run_chat(ctx, event_id: int \| None, history, question) -> AsyncIterator[AgentStep \| ChatAnswer]` | 1 | шаги из `trace.json` с задержкой, затем `insight.json` |
| `app/text/sanitize.py` | `sanitize(text: str) -> str` | 1 | убирает управляющие и невидимые символы |
| `app/clients/profile.py` | `async get_profile(company_id) -> CompanyProfile`; `profile_compact(profile, user_role) -> CompactProfile` (роль входит в сжатый профиль судьи, C2); `feature_phrase(key, value) -> str` | 2 | профиль из фикстур; фраза `"{key}: {value}"` |
| `app/clients/exposures.py` | `exposures(profile, config) -> list[Exposure]` | 2 | `profile.exposures` из фикстуры |
| `app/clients/counterparties.py` | `async get_counterparties(ctx, direction, sector=None, top=10) -> list[CounterpartyShare]`; `get_sector_exposure(sector, factor) -> Exposure \| None` | 2 | из фикстур |
| `app/rank/feed.py` | `async rank_feed(ctx, period, filters) -> list[FeedItem]`; `async explain(ctx, event_id) -> RelevanceScore`; `async highlights(ctx) -> list[FeedItem]`; `async warm_list() -> list[tuple[SessionCtx, int]]` | 2 | фикстурная лента по 3 событиям |
| `app/news/search.py` | `async search_news(query, days=90, factor=None) -> list[NewsHit]`; `async search_bank_chunks(query, factor=None, since=None) -> list[BankChunk]`; `async get_event_sources(event_id, hint) -> list[SourceFragment]` | 3 | из фикстур |
| `app/collect/market.py` | `async get_market_series(series, days) -> MarketSeries` | 3 | ряд из фикстуры |

Инструменты агента (`app/agent/tools.py`, роль 1) строятся поверх этих функций и замыкают `SessionCtx` — аргумента `company_id` у них нет.

## 6. API

- Маршруты — полный список из ТЗ роли 3 (BE-8), префикс `/api`. Схемы ответов — `app/api/schemas.py`, собираются из моделей контракта.
- `MOCK=1` — все маршруты отдают фикстуры, SSE-поток разбора — шаги `trace.json` с задержкой 300–800 мс. Роль 4 работает на этом режиме с первого дня.
- SSE (`text/event-stream`): события `step` (`AgentStep`), `insight` (`InsightResult`), `answer` (ответ чата), `error` (тело ошибки р. 1), `done`. У каждого — `id:` по порядку; переподключение с `Last-Event-ID` получает пройденные шаги заново.
- История чата выбирается по `chat_id` и `user_id` из сессии одновременно: `chat_id` задаёт клиент, и без второго условия чужой чат читался бы по UUID.
- Вход: httpOnly-cookie `orientir_session`; изменяющие запросы требуют заголовок `X-CSRF-Token` (значение отдаёт `/api/me`).
- OpenAPI: `orientir openapi` пишет `docs/openapi.json`; CI падает, если файл устарел. Типы фронта — `openapi-typescript` → `web/src/api/schema.d.ts` командой `npm run gen:api`; руками не правим.

## 7. Фикстуры

Один каталог `tests/fixtures/`, всё валидируется моделями контракта отдельным тестом — фикстура, не совпавшая со схемой, роняет CI. Сквозной сюжет — из примера разбора общего ТЗ (р. 9, F10):

- компании: `C-DEV-01` — девелопер с кредитами и гарантиями; `C-IT-01` — IT-компания с депозитами без кредитов (пара для экрана «Сравнение»);
- события: `1` — ЦБ сохранил ключевую ставку 14% (`regulator_decision`, `key_rate`); `2` — укрепление юаня (`currency`, `cny_rub`); `3` — повышение экспортных пошлин на металлы (`taxes_law`, `export_duties`, `steel`);
- 10 тезисов банка по `key_rate`, `inflation`, `cny_rub`, `steel` с документом и страницей;
- `insight.json` — разбор события 1 для `C-DEV-01` с ролью `cfo`; `trace.json` — его трасса (все виды шагов);
- лента и «почему у вас» для обеих компаний, ряды `cbr_key_rate` и `cbr_cny` за 3 месяца.

Числа и цитаты в фикстурах берутся из материалов кейса и пометок «условные» не требуют только там, где совпадают с кейсом; остальное помечено в `tests/fixtures/README.md` как вымышленное.

## 8. Каркас и окружение

- Каталоги — по общему ТЗ, р. 3.3; Python-пакет `app`, CLI `orientir` (typer) с командами из ТЗ роли 3 (BE-1), пока не реализованные — понятное «ещё не реализовано (пункт ТЗ)».
- `pyproject.toml` + `uv.lock`, `.python-version` = 3.12; ruff (правила `E, F, W, I, B, UP, ASYNC`, длина строки 110); pytest + pytest-asyncio + respx; тесты с БД — на отдельной `TEST_DATABASE_URL`.
- `app/settings.py` (pydantic-settings), `deploy/.env.example` с комментарием к каждой переменной: `DATABASE_URL`, `TEST_DATABASE_URL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_DAILY_BUDGET_RUB=480`, `INSIGHT_MAX_RUB=4`, `CHAT_MAX_RUB=1.5`, `MOCK=0`, `EMBED_ENABLED=0`, `WARM_ENABLED=1`, `SESSION_TTL_HOURS`, `DEMO_PASSWORD`, `CORS_ORIGIN`.
- `deploy/docker-compose.yml`: `db` (pgvector/pgvector:pg16, healthcheck по TCP), `migrate` (тот же образ, `orientir migrate`), `api`, `worker`; один `Dockerfile`, образ без torch; версии образов закреплены.
- `Makefile`: `up`, `down`, `migrate`, `test`, `lint`, `openapi`, `eval` (пока заглушка).
- CI (GitHub Actions, экшены закреплены по SHA): ruff, pytest с сервисом Postgres, проверка свежести `docs/openapi.json`, сборка образа; задача веба включается, когда появится `web/package.json`.
- `data/README.md` — куда положить CSV и PDF организаторов (сами файлы вне git).

## 9. Процесс

- Просмотр вторым по умолчанию — пары 1 ↔ 2 и 3 ↔ 4; PR, который трогает контракт, дополнительно одобряет владелец затронутой части.
- Каркас (р. 8) — зона роли 3; фундамент собирает роль 1, роль 3 принимает его ревью и дальше владеет.
