# Справочник конфигурации

Все параметры задаются в `configs/config.yaml` с возможностью переопределения через ENV.

## Приоритет

```
ENV variables > config.yaml > defaults
```

## victoria_metrics

Параметры подключения к VictoriaMetrics.

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `url` | string | `""` | Базовый URL API VM |
| `timeout` | duration | `60s` | Таймаут HTTP-запросов |
| `max_concurrent` | int | `8` | Макс. параллельных запросов (semaphore) |
| `rps` | float | `20` | Лимит запросов в секунду (token bucket) |
| `retries` | int | `3` | Количество повторных попыток |
| `base_backoff` | float | `0.3` | Начальный бэкoff (сек) |
| `max_backoff` | float | `5.0` | Максимальный бэкoff (сек) |

```yaml
victoria_metrics:
  url: "http://victoria-metrics:8428"
  timeout: 60s
  max_concurrent: 8
  rps: 20
  retries: 3
  base_backoff: 0.3
  max_backoff: 5.0
```

## targets

Список целевых хостов для анализа.

| Параметр | Тип | Обязательный | Описание |
|----------|-----|-------------|----------|
| `name` | string | да | Человекочитаемое имя |
| `instance` | string | да | Метка `instance` в VM |
| `mountpoints` | list | нет | Точки монтирования (по умолчанию `["/"]`) |
| `description` | string | нет | Описание хоста |
| `url` | string | нет | URL для blackbox-проверок |

```yaml
targets:
  - name: "web-server-01"
    instance: "10.0.1.1:9100"
    mountpoints:
      - "/"
      - "/var/lib"
    description: "Production web server"
    url: "https://example.com/health"
```

## analysis

Какие метрики собирать и за какие периоды.

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `cpu` | bool | `true` | Собирать CPU |
| `memory` | bool | `true` | Собирать память |
| `disk` | bool | `true` | Собирать диски |
| `oom` | bool | `true` | Собирать OOM-kill |
| `periods` | list | `["1d", "7d", "14d"]` | Временные окна |

```yaml
analysis:
  cpu: true
  memory: true
  disk: true
  oom: true
  periods:
    - "1d"
    - "7d"
    - "14d"
```

## containers

Настройки анализа Docker/Kubernetes-контейнеров.

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `enabled` | bool | `false` | Включить анализ |
| `change_threshold` | float | `5` | Порог notable по изменению (%) |
| `high_threshold` | float | `70` | Порог notable по потреблению (%) |
| `cpu_threshold` | float | `80` | Порог hot по CPU (%) |
| `mem_threshold` | float | `95` | Порог hot по памяти (%) |
| `filters` | dict | `{}` | Фильтры по лейблам |
| `group_labels` | string | `"envir, job, instance, name, department"` | Labels для `group by` |

```yaml
containers:
  enabled: true
  change_threshold: 5
  high_threshold: 70
  cpu_threshold: 80
  mem_threshold: 95
  filters:
    envir: "prod"
  group_labels: "envir, job, instance, name, department"
```

## blackbox

Настройки проверок доступности эндпоинтов.

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `job` | string | `"blackbox"` | Имя job в VM |
| `ok_threshold` | float | `99.99` | Мин. uptime (%) для статуса "ok" |

```yaml
blackbox:
  job: "blackbox"
  ok_threshold: 99.99
```

## scheduler

Параметры планировщика.

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `analyze_cron` | string | `"0 0 * * *"` | Cron для анализа |
| `send_cron` | string | `"0 8 * * *"` | Cron для отправки |
| `jitter` | duration | `30s` | Макс. jitter перед выполнением |
| `analyze_timeout` | int | `300` | Таймаут анализа (сек) |

```yaml
scheduler:
  analyze_cron: "0 0 * * *"
  send_cron: "0 8 * * *"
  jitter: 30s
  analyze_timeout: 300
```

## notifier

Настройки уведомлений.

| Параметр | Путь | Описание |
|----------|------|----------|
| `enabled` | `notifier.botx.enabled` | Включить уведомления |
| `api_url` | `notifier.botx.api_url` | URL API мессенджера |
| `chat_id` | `notifier.botx.chat_id` | ID чата (задаётся через ENV) |
| `bearer_token` | `notifier.botx.bearer_token` | Токен (задаётся через ENV) |

```yaml
notifier:
  botx:
    enabled: true
    api_url: "https://botx.example.com/api/v4/botx/notifications/direct"
    # chat_id и bearer_token задаются через ENV
```

## storage

Параметры хранилища.

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `path` | string | `"data/infra_stats.db"` | Путь к SQLite |
| `max_reports` | int | `100` | Макс. отчётов в истории |
| `max_notifications` | int | `50` | Макс. уведомлений в истории |

## security

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `api_key` | string | `""` | API-ключ для write-эндпоинтов |

## cache

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `ttl` | int | `300` | Время жизни кэша (сек) |
| `maxsize` | int | `2000` | Макс. записей в кэше |

## ENV-переменные

| Переменная | Описание | Перезаписывает |
|------------|----------|---------------|
| `CONFIG_PATH` | Путь к YAML-конфигу | — |
| `STORAGE_PATH` | Путь к SQLite | `storage.path` |
| `BOTX_BEARER_TOKEN` | Токен мессенджера | `notifier.botx.bearer_token` |
| `BOTX_CHAT_ID` | ID чата | `notifier.botx.chat_id` |
| `API_KEY` | API-ключ | `security.api_key` |
| `PORT` | Порт сервера | `8080` |
| `LOG_LEVEL` | Уровень логов | `info` |
| `LOG_FORMAT` | Формат логов | `text` |
| `TZ` | Часовой пояс | `UTC` |

## Формат duration

Временные значения поддерживают Go-стиль:

| Запись | Значение |
|--------|----------|
| `30s` | 30 секунд |
| `1m` | 1 минута |
| `500ms` | 500 миллисекунд |
| `2h` | 2 часа |
| `60` | 60 секунд (число = секунды) |
