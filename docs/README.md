# infra-stats — Анализатор инфраструктурной нагрузки

Сервис для автоматического сбора и анализа метрик инфраструктуры из VictoriaMetrics,
с отправкой отчётов в мессенджер (Clouds/BotX).

## Назначение

- Сбор CPU, RAM, дисков, OOM-событий для заданных хостов
- Анализ Docker/Kubernetes-контейнеров
- Blackbox-проверки доступности эндпоинтов
- Формирование дифф-отчётов (изменения за период)
- Cron-рассылка в мессенджер
- Веб-интерфейс для просмотра статуса

## Быстрый старт

```bash
# 1. Скопировать конфиг
cp configs/config.yaml configs/config.yaml.local

# 2. Задать секреты
export BOTX_BEARER_TOKEN="your-token"
export BOTX_CHAT_ID="your-chat-id"

# 3. Запустить
docker compose up -d

# 4. Открыть веб-интерфейс
open http://localhost:8080
```

## Структура проекта

```
infra-stats/
├── app/                          # Основной код приложения
│   ├── main.py                   # Точка входа, FastAPI app factory
│   ├── logger.py                 # Настройка логирования (JSON/text)
│   ├── config/
│   │   ├── config.py             # Dataclass-ы конфигурации + загрузчик YAML
│   │   └── manager.py            # Потокобезопасный менеджер конфигурации
│   ├── vmclient/
│   │   └── vmclient.py           # HTTP-клиент к VictoriaMetrics
│   ├── analyzer/
│   │   ├── analyzer.py           # Движок анализа метрик + dataclass-ы отчётов
│   │   ├── container.py          # Анализ Docker/K8s контейнеров
│   │   └── blackbox.py           # Blackbox-проверки эндпоинтов
│   ├── scheduler/
│   │   └── scheduler.py          # Cron-планировщик (APScheduler)
│   ├── storage/
│   │   └── storage.py            # Хранилище отчётов (SQLite)
│   ├── notifier/
│   │   └── notifier.py           # Отправка отчётов в мессенджер
│   ├── handlers/
│   │   └── handlers.py           # REST API эндпоинты
│   └── web/
│       └── static/               # Веб-интерфейс (HTML/JS/CSS)
├── configs/
│   ├── config.yaml               # Основной конфиг
│   ├── vm-scrape.yml             # Конфиг скрейпинга VM
│   └── blackbox.yml              # Конфиг blackbox-exporter
├── docs/                         # Документация
│   ├── README.md                 # Этот файл
│   ├── architecture.md           # Архитектура с Mermaid-схемами
│   ├── configuration.md          # Справочник конфигурации
│   ├── api.md                    # REST API reference
│   ├── deployment.md             # Руководство по деплою
│   └── metrics-queries.md        # PromQL-запросы к VM
├── Dockerfile                    # Multi-stage Docker-билд
├── docker-compose.yml            # Оркестрация всех сервисов
└── requirements.txt              # Python-зависимости
```

## Ключевые документы

| Документ | Описание |
|----------|----------|
| [architecture.md](architecture.md) | Полная архитектура с Mermaid-схемами |
| [configuration.md](configuration.md) | Все параметры конфигурации |
| [api.md](api.md) | REST API reference |
| [deployment.md](deployment.md) | Руководство по деплою |
| [metrics-queries.md](metrics-queries.md) | PromQL-запросы к VictoriaMetrics |

## Технологии

- **Python 3.11** + FastAPI + uvicorn
- **VictoriaMetrics** — хранение и запрос метрик
- **SQLite** — локальное хранение отчётов
- **APScheduler** — cron-планировщик
- **httpx** — async HTTP-клиент
- **Docker** — контейнеризация
