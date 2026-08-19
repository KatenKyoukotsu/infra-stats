# REST API Reference

Base URL: `http://localhost:8080`

## Healthcheck

### `GET /healthcheck`

Проверка работоспособности сервиса.

**Response:** `200 OK`
```
ItsOK
```

---

## Read-эндпоинты (доступны без API-ключа)

### `GET /api/status`

Последний отчёт анализа (с контейнерами, если включены).

**Response:** `200 OK`
```json
{
  "timestamp": "2025-01-15T00:00:00",
  "targets": [
    {
      "name": "web-server-01",
      "cpu": [{"period": "1d", "value": 45.2}],
      "memory": [{"period": "1d", "value": 67.8}],
      "disks": [{"mountpoint": "/", "metrics": [{"period": "1d", "value": 55.0}]}],
      "oom": []
    }
  ],
  "containers": []
}
```

### `GET /api/reports`

Все отчёты из истории.

**Response:** `200 OK` — массив отчётов

### `GET /api/containers`

Контейнеры из последнего отчёта.

**Response:** `200 OK`
```json
[
  {
    "name": "nginx",
    "instance": "10.0.1.1:9100",
    "job": "cadvisor",
    "cpu": [{"period": "1d", "value": 12.5}],
    "memory": [{"period": "1d", "value": 45.0}]
  }
]
```

### `GET /api/config`

Текущая конфигурация (без секретов).

**Response:** `200 OK` — Config.to_dict()

### `GET /api/scheduler`

Статус планировщика.

**Response:** `200 OK`
```json
{
  "analyze_cron": "0 0 * * *",
  "send_cron": "0 8 * * *",
  "status": {
    "analyze": {"last_run": "2025-01-15T00:00:00", "last_success": true},
    "send": {"last_run": "2025-01-15T08:00:00", "last_success": true}
  }
}
```

### `GET /api/notifications`

История уведомлений.

**Response:** `200 OK`
```json
[
  {
    "timestamp": "2025-01-15T08:00:00",
    "success": true,
    "chat_id": "12345"
  }
]
```

### `GET /api/preview`

Предпросмотр текста отчёта для мессенджера.

**Response:** `200 OK`
```json
{
  "text": "📊 *Infra Stats Report*\n🕒 *Time:* 2025-01-15 00:00:00\n...",
  "timestamp": "2025-01-15T00:00:00"
}
```

---

## Write-эндпоинты (требуют API-ключа)

API-ключ передаётся через заголовок:
```
X-API-Key: your-api-key
```
или
```
Authorization: Bearer your-api-key
```

Если `api_key` не задан в конфиге — авторизация отключена.

### `POST /api/analyze`

Запуск анализа вручную.

**Response:** `200 OK` — AnalysisReport.to_dict()

### `POST /api/clear`

Очистка всех отчётов и уведомлений.

**Response:** `200 OK`
```json
{"status": "cleared"}
```

---

## Test-эндпоинты

### `POST /api/test/vm`

Проверка 연결ности с VictoriaMetrics.

**Response:** `200 OK`
```json
{"success": true}
```
или
```json
{"success": false, "error": "vm ping failed: ..."}
```

### `POST /api/test/clouds`

Проверка конфигурации мессенджера.

**Response:** `200 OK`
```json
{"success": true, "api_url": "https://...", "chat_id": "12345"}
```

### `POST /api/test/send`

Отправка тестового отчёта в мессенджер.

**Response:** `200 OK`
```json
{"success": true}
```

---

## Статика

`GET /` — веб-интерфейс (HTML/JS/CSS из `app/web/static/`).

## Коды ошибок

| Код | Описание |
|-----|----------|
| `200` | Успех |
| `403` | Невалидный API-ключ |
| `404` | Отчёты ещё не сгенерированы |
| `500` | Внутренняя ошибка сервера |
