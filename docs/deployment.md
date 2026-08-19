# Руководство по деплою

## Предварительные требования

- Docker + Docker Compose v2
- Доступ к VictoriaMetrics (URL)
- BotX/Clouds API token и chat_id

## Быстрый старт (dev)

```bash
# 1. Клонировать репозиторий
git clone <repo-url> && cd infra-stats

# 2. Задать секреты
export BOTX_BEARER_TOKEN="your-botx-token"
export BOTX_CHAT_ID="your-chat-id"

# 3. Запустить
docker compose up -d

# 4. Проверить
curl http://localhost:8080/healthcheck
```

## Продакшен

### 1. Конфигурация

Скопируйте конфиг и настройте под ваше окружение:

```bash
cp configs/config.yaml configs/config.yaml.prod
```

Измените:
- `victoria_metrics.url` — URL вашей VM
- `targets` — список ваших хостов
- `analysis.periods` — нужные периоды
- `containers.enabled: true` + `containers.filters` — если нужен анализ контейнеров
- `scheduler.analyze_cron` / `send_cron` — расписание

### 2. Секреты

**Никогда** не храните секреты в config.yaml! Используйте ENV:

```bash
# В .env файле или в orchestration (K8s secrets, Docker secrets)
BOTX_BEARER_TOKEN=your-token
BOTX_CHAT_ID=your-chat-id
API_KEY=your-api-key  # опционально, для защиты write-эндпоинтов
```

### 3. Docker Compose (prod)

```yaml
services:
  infra-stats:
    <<: *common
    image: your-registry/infra-stats:latest
    environment:
      - CONFIG_PATH=/app/configs/config.yaml
      - STORAGE_PATH=/app/data/infra_stats.db
      - BOTX_BEARER_TOKEN=${BOTX_BEARER_TOKEN}
      - BOTX_CHAT_ID=${BOTX_CHAT_ID}
      - API_KEY=${API_KEY}
      - LOG_LEVEL=info
      - LOG_FORMAT=json
    volumes:
      - ./configs:/app/configs:ro
      - statsdata:/app/data
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.5"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

### 4. VictoriaMetrics (300+ VM)

Для продуктивной VM с 300+ инстансами:

```yaml
victoria_metrics:
  url: "http://your-vm:8428"
  timeout: 60s
  max_concurrent: 8    # параллельных запросов
  rps: 20              # запросов в секунду
  retries: 3
```

**Рекомендации:**
- `max_concurrent: 8` — оптимально для 300 VM (один запрос = все инстансы)
- `rps: 20` — не нагружает VM (12 запросов за ~0.6 сек)
- `cache.ttl: 300` — кэш 5 мин, повторные запросы мгновенны
- Контейнеры: включать только если нужен детальный анализ

### 5. Мониторинг

Сервис экспортит метрики через node-exporter. Дополнительно:

```yaml
# Добавить в vm-scrape.yml для мониторинга infra-stats
- job_name: "infra-stats"
  scrape_interval: 30s
  static_configs:
    - targets: ["infra-stats:8080"]
```

### 6. Резервное копирование

SQLite-база хранится в Docker volume `statsdata`:

```bash
# Бэкап
docker exec infra-stats sqlite3 /app/data/infra_stats.db ".backup /app/data/backup.db"
docker cp infra-stats:/app/data/backup.db ./backups/

# Восстановление
docker cp ./backups/backup.db infra-stats:/app/data/infra_stats.db
docker restart infra-stats
```

## Env-переменные

| Переменная | Обязательная | По умолчанию | Описание |
|------------|-------------|-------------|----------|
| `CONFIG_PATH` | нет | `configs/config.yaml` | Путь к конфигу |
| `STORAGE_PATH` | нет | `data/infra_stats.db` | Путь к SQLite |
| `BOTX_BEARER_TOKEN` | да* | — | Токен мессенджера |
| `BOTX_CHAT_ID` | да* | — | ID чата |
| `API_KEY` | нет | — | API-ключ |
| `PORT` | нет | `8080` | Порт сервера |
| `LOG_LEVEL` | нет | `info` | Уровень логов |
| `LOG_FORMAT` | нет | `text` | Формат (json/text) |
| `TZ` | нет | `UTC` | Часовой пояс |

*Обязательны если `notifier.botx.enabled: true`

## Обновление

```bash
# 1. Обновить образ
git pull
docker compose build infra-stats

# 2. Перезапустить
docker compose up -d infra-stats

# 3. Проверить
curl http://localhost:8080/api/status
```

## Troubleshooting

### Сервис не стартует

```bash
docker compose logs infra-stats
# Проверить: правильный ли URL к VM, доступен ли он
```

### Нет данных

```bash
# Проверить VM
curl http://localhost:8428/api/v1/query?query=up

# Проверить targets в конфиге
curl http://localhost:8080/api/config
```

### Уведомления не отправляются

```bash
# Проверить конфигурацию мессенджера
curl -X POST http://localhost:8080/api/test/clouds

# Проверить отправку
curl -X POST http://localhost:8080/api/test/send
```
