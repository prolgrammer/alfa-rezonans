# Данные организаторов

Сами файлы в репозиторий не попадают (`.gitignore`: всё в `data/`, кроме этого файла) — отдавать их в git можно только с разрешения организаторов.

## Куда класть

| Что | Куда |
| --- | --- |
| Три CSV с клиентами, их физлицами и платежами | `data/clients/` |
| Девять PDF с аналитикой банка | `data/bank/` |

## Как загрузить

```bash
orientir load-clients data/clients
orientir ingest-bank data/bank
```

В контейнере то же самое: `docker compose -f deploy/docker-compose.yml exec api orientir load-clients data/clients` (каталог `data/` смонтируйте в контейнер или скопируйте внутрь).

## Без данных организаторов

Сервис работает на синтетических данных: `examples/synthetic/` (см. DQ-5). Демо-режим `orientir demo` берёт их автоматически, если папки выше пусты.
