# Ориентир

## Первый экран

_Раздел принадлежит роли 4: описание продукта, скриншоты и ссылка на демо._

## Запуск для разработки

Нужны Python 3.12 с [uv](https://docs.astral.sh/uv/), Docker и GNU Make (на Windows — Git Bash).

```bash
make env                  # deploy/.env из примера; поправьте пароли и порты
uv sync
make db                   # только PostgreSQL с pgvector
make migrate              # применить миграции
uv run uvicorn app.api.main:app --reload
```

Весь стек в контейнерах: `make up` (api на `http://localhost:8000`), остановить — `make down`.

### Режим MOCK

`MOCK=1` в `deploy/.env` — все маршруты `/api` отдают данные из `tests/fixtures/`, база и модель не нужны. Так интерфейс разрабатывается с первого дня:

- сессия одна и фиксированная: клиент `C-DEV-01`, роль `cfo`, права `expert`; `POST /api/demo/switch` переключает клиента и роль на `C-IT-01` и обратно;
- вход обязателен: `POST /api/auth/login` с любым логином и паролем из `DEMO_PASSWORD` ставит cookie `orientir_session` (значение любое, сессия по-прежнему одна); без cookie и после `POST /api/auth/logout` маршруты отвечают 401 `unauthorized`, открыты только `GET /api/health` и вход;
- изменяющие запросы требуют заголовок `X-CSRF-Token` со значением из `GET /api/me` (в MOCK оно постоянное: `mock-csrf-token`);
- разбор и чат отдаются потоком SSE (`GET /api/insights/{id}/stream`, `POST /api/chat`) с паузой 0,3–0,8 с между шагами; кадры пронумерованы, после обрыва клиент присылает `Last-Event-ID` и получает только то, что осталось;
- в MOCK номер разбора равен номеру события, а разбор есть только у двух пар «клиент × событие 1»: `C-DEV-01` (риск, ставка) и `C-IT-01` (возможность, доходность депозитов). Для остальных пар `POST /api/events/{id}/insight`, `/api/insights/{id}/stream` и `/trace` отвечают 404 `not_found`; экран «Сравнение» по событию 1 получает два разных разбора. Событий в фикстурах три (`1`, `2`, `3`);
- поток SSE заканчивается кадром `done`: закрывайте `EventSource` по нему. Если клиент всё же переподключился с `Last-Event-ID` последнего кадра, сервер отвечает 204 и `EventSource` останавливается;
- ошибки всегда имеют вид `{"error": {"code", "message", "details"}}`.

Схема API — `docs/openapi.json`, интерактивная документация — `http://localhost:8000/docs`. Типы для веба генерируются из схемы; после правки маршрутов или моделей выполните `uv run orientir openapi` и закоммитьте файл: CI сверяет его с кодом.

## Проверки

```bash
make test                          # ruff, ruff format --check, pytest
uv run orientir openapi --check    # схема API свежая
```

Тесты с базой читают `TEST_DATABASE_URL` из окружения (не из `deploy/.env`): база должна называться `orientir*_test`, её схема пересоздаётся при каждом тесте. Если задана только основная база, создайте вторую и укажите её порт. Без переменной тесты с БД пропускаются, `pytest -rs` покажет причину.

## Устройство

| Каталог | Что внутри |
| --- | --- |
| `app/contracts/` | общие модели данных: единый контракт четырёх ролей |
| `app/api/` | FastAPI: маршруты, SSE, ошибки; `MockBackend` (фикстуры) и `LiveBackend` (функции ролей) |
| `app/llm/`, `app/agent/`, `app/news/`, `app/bank/`, `app/text/` | зона роли 1 |
| `app/clients/`, `app/rank/`, `app/eval/` | зона роли 2 |
| `app/db.py`, `migrations/`, `app/collect/`, `app/worker/` | зона роли 3 |
| `config/` | справочники факторов, отраслей, моделей, промптов |
| `tests/fixtures/` | фикстуры сквозного сюжета; описание — `tests/fixtures/README.md` |
| `deploy/` | docker compose и пример окружения |

Правила работы — [CONTRIBUTING.md](CONTRIBUTING.md).
