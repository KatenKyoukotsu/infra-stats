# Infra Stats Analyzer

Сервис для автоматизированного анализа метрик инфраструктуры из VictoriaMetrics. Собирает статистику CPU, памяти и дисков через PromQL-запросы к VictoriaMetrics, сохраняет историю в памяти и отправляет сводные отчёты в BotX по расписанию.

---

## Архитектура

```mermaid
flowchart TD
    subgraph VM["VictoriaMetrics"]
        direction TB
        TSDB[("Time-Series DB")]
    end

    subgraph InfraStats["Infra Stats Analyzer"]
        direction TB
        Config["Config Manager\n(configs/config.yaml)"]
        Engine["Analyzer Engine\n(PromQL Queries)"]
        Storage[("In-Memory Storage\n(Last N Reports)")]
        Scheduler["Cron Scheduler\n(robfig/cron)"]
        Notifier["BotX Client"]
        HTTP["HTTP Server\n(REST API + Web UI)"]
    end

    subgraph External["External Systems"]
        Browser["Web Browser\n(Debug Console)"]
        BotX["BotX Messenger"]
    end

    VM -->|"PromQL query"| Engine
    Scheduler -->|"analyze_cron"| Engine
    Scheduler -->|"send_cron"| Notifier
    Engine -->|"write"| Storage
    Storage -->|"read"| Notifier
    Storage -->|"read"| HTTP
    HTTP -->|"serve"| Browser
    Notifier -->|"HTTP POST"| BotX
    Config -->|"targets + analysis config"| Engine
    Config -->|"schedule"| Scheduler
    Config -->|"botx config"| Notifier
```

---

## Структура проекта

```text
.
├── cmd/
│   └── main.go                       # Точка входа, роутинг, graceful shutdown
├── configs/
│   ├── config.yaml                   # Основной файл конфигурации
│   └── vm-scrape.yml                 # (dev) Конфиг скрапинга для VM
├── docs/
│   └── source/
│       └── index.md                  # Документация
├── internal/
│   ├── analyzer/                     # PromQL-запросы и анализ метрик
│   │   └── analyzer.go              # Engine, TargetStats, AnalysisReport, DiskStat
│   ├── config/                       # Менеджер конфигурации (YAML)
│   │   ├── config.go                # Структуры + LoadConfig
│   │   └── manager.go               # Thread-safe Manager (Get/Save)
│   ├── logger/                       # Настройка slog (LOG_LEVEL / LOG_FORMAT)
│   │   └── logger.go
│   ├── notifier/                     # BotX клиент + история отправок (50 записей)
│   │   └── notifier.go
│   ├── scheduler/                    # Крон-планировщик (robfig/cron) + статус JobStatus
│   │   └── sheduler.go
│   ├── storage/                      # In-memory кольцевой буфер
│   │   └── storage.go
│   ├── vmclient/                     # HTTP-клиент к VictoriaMetrics
│   │   └── vmclient.go
│   └── web/                          # Web debug console
│       ├── embed.go                  # Go embed для статики
│       └── static/
│           └── index.html            # SPA-консоль (тёмная тема)
├── Dockerfile                        # Multi-stage сборка (Alpine + Go)
├── docker-compose.yml                # Compose: infra-stats + VM + node-exporter
├── go.mod
└── go.sum
```

---

## Конфигурация (`configs/config.yaml`)

```yaml
victoria_metrics:
  url: "http://localhost:8428"
  timeout: 30s

targets:
  - name: "server-01"
    instance: "192.168.1.10:9100"
    mountpoints:
      - "/"
      - "/var/lib"
      - "/data"
    description: "Main application server"

analysis:
  cpu: true
  memory: true
  disk: true
  oom: true
  periods:
    - "1d"
    - "2d"
    - "7d"

scheduler:
  analyze_cron: "0 0 * * *"
  send_cron: "0 8 * * *"

notifier:
  botx:
    enabled: true
    api_url: "https://botx.example.com/api/v4/botx/notifications/direct"
    chat_id: "${BOTX_CHAT_ID}"
    bearer_token: "${BOTX_BEARER_TOKEN}"
```

### Поля конфигурации

| Поле | Тип | Описание |
|---|---|---|
| `victoria_metrics.url` | string | Адрес VictoriaMetrics (http/https) |
| `victoria_metrics.timeout` | duration | Таймаут запросов к VM |
| `targets[].name` | string | Отображаемое имя сервера |
| `targets[].instance` | string | Instance label в VM (обычно `host:9100`) |
| `targets[].mountpoints` | []string | Список точек монтирования для анализа дисков (если не указан — `/`) |
| `analysis.cpu` | bool | Анализировать CPU |
| `analysis.memory` | bool | Анализировать память |
| `analysis.disk` | bool | Анализировать диск |
| `analysis.oom` | bool | Проверять OOM-события (OOM Killer) |
| `analysis.periods` | []string | Временные окна (1d, 2d, 7d и т.д.) |
| `scheduler.analyze_cron` | string | Cron-расписание запуска анализа |
| `scheduler.send_cron` | string | Cron-расписание отправки отчёта |
| `notifier.botx.enabled` | bool | Включить отправку в BotX |
| `notifier.botx.api_url` | string | URL BotX API |
| `notifier.botx.chat_id` | string | ID чата/группы (переопределяется через `BOTX_CHAT_ID`) |
| `notifier.botx.bearer_token` | string | Токен (переопределяется через `BOTX_BEARER_TOKEN`) |

### PromQL-запросы

| Метрика | Запрос |
|---|---|
| **CPU** (средняя за период) | `100 - avg(rate(node_cpu_seconds_total{mode="idle",instance="..."}[1d])) * 100` |
| **Memory** (средняя за период) | `avg_over_time((1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)[1d:2m]) * 100` |
| **Disk** (средняя за период, per-mountpoint) | `avg_over_time((1 - node_filesystem_avail_bytes{mountpoint="/",instance="..."} / node_filesystem_size_bytes{mountpoint="/",instance="..."})[1d:2m]) * 100` |
| **OOM** (число OOM kill за период) | `sum(increase(node_vmstat_oom_kill{instance="..."}[1d]))` |

---

## REST API Specification

### Endpoints

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/healthcheck` | Healthcheck, возвращает `ItsOK` |
| `GET` | `/api/status` | Последний отчёт анализа |
| `GET` | `/api/reports` | Все сохранённые отчёты |
| `POST` | `/api/analyze` | Запустить анализ принудительно |
| `POST` | `/api/clear` | Очистить историю отчётов |
| `GET` | `/api/config` | Получить текущий конфиг |
| `GET` | `/api/scheduler` | Статус cron-задач (последний запуск, успех/ошибка) |
| `GET` | `/api/notifications` | История отправок уведомлений (макс. 50) |
| `GET` | `/api/preview` | Предпросмотр сообщения для BotX (plain text) |
| `POST` | `/api/test/vm` | Проверка подключения к VictoriaMetrics |
| `POST` | `/api/test/clouds` | Валидация конфигурации Clouds (BotX) |
| `POST` | `/api/test/send` | Отправка тестового сообщения в Clouds |
| `GET` | `/` | Web debug console |

### GET /healthcheck

```
GET /healthcheck
→ 200 OK
Body: ItsOK
```

### GET /api/status

```
GET /api/status
→ 200 OK

{
  "timestamp": "2026-07-29T00:57:34.879004597+03:00",
  "targets": [
    {
      "name": "server-01",
      "cpu": [
        { "period": "1d", "value": 23.4, "diff": 1.2 },
        { "period": "2d", "value": 22.1, "diff": -0.5 },
        { "period": "7d", "value": 20.5 }
      ],
      "memory": [
        { "period": "1d", "value": 62.1, "diff": -0.3 },
        { "period": "2d", "value": 61.8, "diff": -0.7 },
        { "period": "7d", "value": 60.3 }
      ],
      "disks": [
        {
          "mountpoint": "/",
          "metrics": [
            { "period": "1d", "value": 45.2, "diff": 0.4 },
            { "period": "2d", "value": 44.8, "diff": 0.1 },
            { "period": "7d", "value": 43.1 }
          ]
        },
        {
          "mountpoint": "/var/lib",
          "metrics": [
            { "period": "1d", "value": 8.9, "diff": 0.0 },
            { "period": "2d", "value": 8.9, "diff": 0.0 },
            { "period": "7d", "value": 8.9 }
          ]
        }
      ],
      "oom": [
        { "period": "1d", "count": 2, "diff": 1 },
        { "period": "2d", "count": 5 }
      ]
    }
  ]
}
```

→ `404` если отчётов ещё нет:
```json
{ "error": "no reports yet" }
```

### GET /api/reports

```
GET /api/reports
→ 200 OK

[
  { "timestamp": "...", "targets": [...] },
  { "timestamp": "...", "targets": [...] }
]
```

### POST /api/analyze

```
POST /api/analyze
→ 200 OK

{
  "timestamp": "2026-07-29T00:57:34.879004597+03:00",
  "targets": [...]
}
```

Запускает полный цикл анализа для всех целей из конфига и сохраняет результат в историю.

### POST /api/clear

```
POST /api/clear
→ 200 OK

{ "status": "cleared" }
```

### GET /api/config

```
GET /api/config
→ 200 OK

{
  "VictoriaMetrics": { "URL": "http://victoria-metrics:8428", "Timeout": 30000000000 },
  "Targets": [...],
  "Analysis": { "cpu": true, "memory": true, "disk": true, "periods": ["1d","2d","7d"] },
  "Scheduler": { "AnalyzeCron": "0 0 * * *", "SendCron": "0 8 * * *" },
  "Notifier": { "BotX": { "enabled": true, ... } }
}
```

### GET /api/scheduler

```
GET /api/scheduler
→ 200 OK

{
  "analyze_cron": "0 0 * * *",
  "send_cron": "0 8 * * *",
  "status": {
    "analyze": {
      "last_run": "2026-07-29T00:00:00+03:00",
      "last_success": true,
      "last_error": ""
    },
    "send": {
      "last_run": "2026-07-29T08:00:00+03:00",
      "last_success": false,
      "last_error": "botx API returned non-2xx status code: 401"
    }
  }
}
```

Поле `last_error` присутствует только при неудачном выполнении. `last_success=false` с пустой ошибкой означает, что задача ещё ни разу не запускалась.

### GET /api/notifications

```
GET /api/notifications
→ 200 OK

[
  {
    "timestamp": "2026-07-29T08:00:00+03:00",
    "success": true,
    "chat_id": "chat-123"
  },
  {
    "timestamp": "2026-07-29T08:00:00+03:00",
    "success": false,
    "error": "HTTP 401",
    "chat_id": "chat-123"
  }
]
```

Возвращает массив записей (не более 50), отсортированных от старых к новым. Поле `error` присутствует только при неудачной отправке.

---

## Формат отчёта в BotX

```
📊 *Infra Stats Report*
🕒 *Time:* 2026-07-29 14:30:00

🖥 *server-01*
   📈 CPU: 1d: 23.4% (+1.2) | 2d: 22.1% (-0.5) | 7d: 20.5%
   📈 Mem: 1d: 62.1% (-0.3) | 2d: 61.8% (-0.7) | 7d: 60.3%
   💾 root: 1d: 45.2% (+0.4) | 2d: 44.8% (+0.1) | 7d: 43.1%
   💾 /var/lib: 1d: 8.9% (+0.0) | 2d: 8.9% (+0.0) | 7d: 8.9%
   💀 OOM (1d): 2 kill(s) (+1)
   💀 OOM (2d): 5 kill(s)

🖥 *server-02*
   📈 CPU: 1d: 45.2% | 2d: 44.8% | 7d: 43.1%
   📈 Mem: 1d: 78.5% | 2d: 77.9% | 7d: 76.2%
   💾 /data: 1d: 12.8% | 2d: 12.5% | 7d: 11.9%
```

### GET /api/preview

```
GET /api/preview
→ 200 OK

{
  "text": "📊 *Infra Stats Report*\n🕒 *Time:* ...\n\n🖥 *server-01*\n   📈 CPU: 1d: ...",
  "timestamp": "2026-07-29T..."
}
```

→ `404` если отчётов ещё нет:
```json
{ "error": "no reports available" }
```

### POST /api/test/vm

```
POST /api/test/vm
→ 200 OK

{ "success": true }
```

При ошибке:
```json
{ "success": false, "error": "vm ping failed: ..." }
```

### POST /api/test/clouds

```
POST /api/test/clouds
→ 200 OK

{ "success": true, "api_url": "https://...", "chat_id": "chat-xxx" }
```

При ошибке:
```json
{ "success": false, "error": "botx api_url is empty" }
```

### POST /api/test/send

```
POST /api/test/send
→ 200 OK

{ "success": true }
```

Отправляет последний отчёт в BotX, используя текущую конфигурацию. При ошибке возвращает `{ "success": false, "error": "..." }`.

---

Поле `diff` показывает изменение относительно предыдущего отчёта. Значение в скобках — дельта: `(+X.Y)` означает рост (ухудшение), `(-X.Y)` — снижение (улучшение). Для первой записи после запуска diff отсутствует.

---

## Запуск

### Docker Compose (рекомендуемый)

```bash
# Сборка и запуск всех сервисов
docker compose up -d --build

# Проверка статуса
docker compose ps

# Логи
docker compose logs -f infra-stats
```

### Локальный запуск

```bash
# Требуется: Go 1.26+, VictoriaMetrics (локально или удалённо)

go mod tidy
LOG_LEVEL=debug go run cmd/main.go
```

Локальный конфиг по умолчанию: `configs/config.yaml`. Путь можно переопределить через `CONFIG_PATH`.

---

## Переменные окружения

| Переменная | Описание |
|---|---|
| `CONFIG_PATH` | Путь к конфигурационному файлу (по умолч. `configs/config.yaml`) |
| `LOG_LEVEL` | `debug`, `info`, `warn`, `error` (по умолч. `info`) |
| `LOG_FORMAT` | `text` или `json` (по умолч. `text`) |
| `BOTX_BEARER_TOKEN` | Bearer-токен для BotX (переопределяет значение из YAML) |
| `BOTX_CHAT_ID` | ID чата BotX (переопределяет значение из YAML) |

---

## Безопасность

1. **In-memory storage**: история отчётов хранится только в памяти, не пишется на диск (кроме конфига).
2. **Токены через ENV**: Bearer-токен и Chat ID можно задать через переменные окружения — они имеют приоритет над YAML и не светятся в репозитории.
3. **Docker**: контейнер запускается от непривилегированного пользователя `appuser`, сброшены все capabilities (`cap_drop: ALL`), запрещено повышение привилегий (`no-new-privileges=true`).
4. **VictoriaMetrics**: используется как read-only источник данных — запись метрик не производится.
