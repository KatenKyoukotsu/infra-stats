# PromQL-запросы к VictoriaMetrics

Документация всех PromQL-запросов, которые infra-stats отправляет к VictoriaMetrics.

## Архитектура запросов

- Все запросы — instant query (`/api/v1/query`)
- Запросы батчатые: **1 запрос на метрику на период** (не на каждый хост)
- Результаты кэшируются в TTLCache (TTL=300s, maxsize=2000)
- Все запросы проходят через semaphore (max=8) и rate-limiter (rps=20)

## Метрики хостов (VM-level)

### CPU

**Запрос:**
```promql
100 - avg by (instance) (
  rate(node_cpu_seconds_total{mode="idle",instance=~"vm1|vm2|..."}[1d])
) * 100
```

**Описание:** Средняя загрузка CPU за период. Вычисляется как 100 - idle%.

**Параметры:**
- `instance=~"..."` — regex-селектор всех target-хостов
- `[1d]` / `[7d]` / `[14d]` — временные окна

### Память

**Запрос:**
```promql
(1 - avg_over_time(node_memory_MemAvailable_bytes{instance=~"vm1|vm2|..."}[1d])
 / avg_over_time(node_memory_MemTotal_bytes{instance=~"vm1|vm2|..."}[1d])) * 100
```

**Описание:** Средний % использования памяти. Отношение средних (не субзапрос).

### Диски

**Запрос:**
```promql
avg by (instance, mountpoint) (
  (1 - avg_over_time(
    node_filesystem_avail_bytes{
      instance=~"vm1|vm2|...",
      mountpoint=~"^(/|/var/lib)$",
      fstype!~"tmpfs|overlay|squashfs|ramfs|cgroup|devtmpfs"
    }[1d])
  / avg_over_time(
    node_filesystem_size_bytes{
      instance=~"vm1|vm2|...",
      mountpoint=~"^(/|/var/lib)$",
      fstype!~"tmpfs|overlay|squashfs|ramfs|cgroup|devtmpfs"
    }[1d])
  ) * 100)
```

**Описание:** Средний % использования дисков по mountpoint.

**Фильтры:**
- `mountpoint=~"^(/|/var/lib)$"` — только нужные точки монтирования
- `fstype!~"tmpfs|overlay|..."` — исключаем виртуальные ФС

### OOM-kill

**Запрос:**
```promql
sum by (instance) (
  increase(node_vmstat_oom_kill{instance=~"vm1|vm2|..."}[1d])
)
```

**Ооличество OOM-kill событий за период.**

## Blackbox-проверки

### Uptime

**Запрос:**
```promql
avg_over_time(probe_success{job="blackbox"}[1d]) * 100
```

**Описание:** % успешных probe за период.

**Статусы:**
- `ok`: uptime >= 99.99% (настраивается через `blackbox.ok_threshold`)
- `down`: uptime < 99.99%
- `unmonitored`: нет данных

## Контейнеры (опционально)

### CPU Limit ( cores)

**Запрос (инициализация):**
```promql
sum by (envir, job, instance, name, department) (
  (container_spec_cpu_quota{...} > 0) / (container_spec_cpu_period{...} > 0)
)
```

**Описание:** CPU-лимит контейнера в ядрах.

### CPU Usage

**Запрос:**
```promql
sum by (envir, job, instance, name, department) (
  rate(container_cpu_usage_seconds_total{...}[1d])
)
```

**Описание:** Средний CPU usage контейнера (ядро-секунды/секунду).

**Вычисление %:**
- `cpu = (usage / limit) * 100` — % от лимита контейнера
- `cpu_vm = (usage / node_cores) * 100` — % от ядер ноды

### Memory Ratio

**Запрос:**
```promql
(avg_over_time(container_memory_working_set_bytes{...}[1d])
 / avg_over_time(container_spec_memory_limit_bytes{...}[1d])) * 100
```

**Описание:** % использования памяти от лимита контейнера.

### Working Set Average

**Запрос:**
```promql
avg_over_time(container_memory_working_set_bytes{...}[1d])
```

**Вычисление % от ноды:**
```promql
mem_vm = (working_set / node_memory_MemTotal_bytes) * 100
```

## Node Info (инициализация контейнеров)

### CPU Cores

```promql
count by (instance) (node_cpu_seconds_total{instance=~"..."})
```

### Memory Total

```promql
node_memory_MemTotal_bytes{instance=~"..."}
```

## Пример: 300 VM, 3 периода

| Метрика | Запросов | Итого |
|---------|----------|-------|
| CPU | 3 (по периодам) | 3 |
| Memory | 3 | 3 |
| Disk | 3 | 3 |
| OOM | 3 | 3 |
| Endpoints | 3 | 3 |
| **VM итого** | | **15** |
| Container init | 3 | 3 |
| Container metrics | 3×3=9 | 9 |
| **Контейнеры итого** | | **12** |
| **ВСЕГО** | | **27** |

При `rps=20` и `max_concurrent=8` — все 27 запросов выполняются за ~1.5 секунды.
