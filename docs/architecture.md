# Архитектура infra-stats

## Обзор системы

```mermaid
graph TB
    subgraph "infra-stats stack"
        WEB["Web UI<br/>(HTML/JS)"]
        API["FastAPI<br/>(port 8080)"]
        SCHED["APScheduler<br/>(cron jobs)"]
        ENGINE["Analysis Engine<br/>(async queries)"]
        VMCLIENT["VmClient<br/>(httpx + semaphore)"]
        STORAGE["SQLite Storage<br/>(WAL mode)"]
        NOTIFY["Notifier<br/>(BotX/Clouds)"]
    end

    subgraph "Внешние сервисы"
        VM["VictoriaMetrics<br/>(port 8428)"]
        NE["Node Exporter<br/>(port 9100)"]
        BB["Blackbox Exporter<br/>(port 9115)"]
        BOTX["BotX/Clouds<br/>(messenger)"]
    end

    WEB --> API
    SCHED --> ENGINE
    API --> ENGINE
    API --> STORAGE
    API --> NOTIFY
    ENGINE --> VMCLIENT
    VMCLIENT --> VM
    NE --> VM
    BB --> VM
    NOTIFY --> BOTX
    ENGINE --> STORAGE
```

## Жизненный цикл данных

```mermaid
sequenceDiagram
    participant C as Cron Scheduler
    participant E as Engine
    participant VM as VictoriaMetrics
    participant S as Storage (SQLite)
    participant N as Notifier
    participant M as Messenger

    Note over C: analyze_cron сработал
    C->>E: run_analysis(targets)
    E->>VM: query(CPU/Mem/Disk/OOM)
    VM-->>E: series[]
    E->>S: add_report(report)
    
    Note over C: send_cron сработал
    C->>S: get_last_report()
    S-->>C: report
    C->>N: send_report(cfg, report)
    N->>M: POST /api/.../notifications/direct
    M-->>N: 200 OK
    N->>S: add_notification(record)
```

## Модульная архитектура

```mermaid
graph LR
    subgraph "Конфигурация"
        CONFIG["config.py<br/>dataclass-ы + loader"]
        MGR["manager.py<br/>thread-safe get/save"]
    end

    subgraph "Ядро анализа"
        ENGINE["analyzer.py<br/>Engine + data models"]
        CONTAINER["container.py<br/>container analysis"]
        BLACKBOX["blackbox.py<br/>endpoint checks"]
    end

    subgraph "Инфраструктура"
        VMCLIENT["vmclient.py<br/>VM HTTP client"]
        STORAGE["storage.py<br/>SQLite + cache"]
        SCHEDULER["scheduler.py<br/>APScheduler cron"]
        NOTIFIER["notifier.py<br/>BotX client"]
        HANDLERS["handlers.py<br/>REST API"]
    end

    CONFIG --> MGR
    MGR --> ENGINE
    MGR --> SCHEDULER
    ENGINE --> VMCLIENT
    ENGINE --> CONTAINER
    ENGINE --> BLACKBOX
    ENGINE --> STORAGE
    SCHEDULER --> ENGINE
    SCHEDULER --> NOTIFIER
    HANDLERS --> ENGINE
    HANDLERS --> STORAGE
    HANDLERS --> SCHEDULER
    NOTIFIER --> STORAGE
```

## Pipeline анализа

```mermaid
flowchart TD
    START["run_analysis(targets)"] --> SELECTOR["instance_selector(targets)"]
    SELECTOR --> PARALLEL
    
    subgraph PARALLEL ["Параллельные циклы (asyncio.gather)"]
        CPU["_loop_cpu<br/>rate(node_cpu_seconds_total)"]
        MEM["_loop_memory<br/>avg_over_time(node_memory_*)"]
        DISK["_loop_disk<br/>avg_over_time(node_filesystem_*)"]
        OOM["_loop_oom<br/>increase(node_vmstat_oom_kill)"]
        EP["_loop_endpoints<br/>avg_over_time(probe_success)"]
    end

    PARALLEL --> MERGE["Сборка AnalysisReport"]
    MERGE --> CACHE["TTLCache<br/>(кэш результатов)"]
    MERGE --> DIFF["compute_diffs<br/>(сравнение с предыдущим)"]
    DIFF --> SAVE["Storage.add_report()"]
```

## Pipeline анализа контейнеров

```mermaid
flowchart TD
    START["run_containers(engine, instances)"] --> INIT
    
    subgraph INIT ["Инициализация (3 параллельных запроса)"]
        Q1["CPU limit cores"]
        Q2["Node CPU cores"]
        Q3["Node memory total"]
    end

    INIT --> LOOP
    
    subgraph LOOP ["Цикл по периодам"]
        PERIOD["Для каждого периода"]
        PERIOD --> Q4["CPU usage<br/>(sum rate)"]
        PERIOD --> Q5["Memory ratio<br/>(working_set / limit)"]
        PERIOD --> Q6["Working set avg<br/>(avg_over_time)"]
    end

    LOOP --> COMPUTE["Вычисление %:<br/>• cpu = usage / limit * 100<br/>• cpu_vm = usage / node_cores * 100<br/>• mem = working_set / limit * 100<br/>• mem_vm = working_set / node_total * 100"]
    COMPUTE --> SORT["Сортировка по (instance, name)"]
```

## Управление конфигурацией

```mermaid
flowchart LR
    YAML["config.yaml"] --> LOAD["load_config()"]
    ENV["ENV vars<br/>BOTX_BEARER_TOKEN<br/>BOTX_CHAT_ID<br/>STORAGE_PATH<br/>API_KEY"] --> LOAD
    LOAD --> CFG["Config (dataclass)"]
    CFG --> MGR["Manager<br/>(thread-safe)"]
    MGR -->|"get()"| APP["FastAPI handlers"]
    MGR -->|"save()"| YAML2["config.yaml<br/>(обновлённый)"]
    MGR -->|"apply_env_overrides()"| CFG2["Config<br/>(с secrets)"]
```

## Потоковая модель

```mermaid
graph TB
    subgraph "Event Loop (main thread)"
        FASTAPI["FastAPI<br/>(async handlers)"]
        SCHEDULER["APScheduler<br/>(async jobs)"]
    end

    subgraph "Worker Threads"
        SQLITE["SQLite<br/>(run_in_executor)"]
    end

    subgraph "HTTP Connections"
        VM["VictoriaMetrics<br/>(semaphore=8, rps=20)"]
        BOTX["BotX API<br/>(timeout=10s)"]
    end

    FASTAPI --> SQLITE
    SCHEDULER --> SQLITE
    SCHEDULER --> VM
    FASTAPI --> BOTX
```

## Кэширование

```mermaid
flowchart TD
    QUERY["PromQL Query"] --> CACHE{"TTLCache<br/>maxsize=2000<br/>ttl=300s"}
    CACHE -->|hit| RESULT["Возврат кэша"]
    CACHE -->|miss| VM["Запрос к VM"]
    VM --> STORE["Сохранение в кэш"]
    STORE --> RESULT
```

## Обработка ошибок и retry

```mermaid
flowchart TD
    REQ["HTTP Request"] --> OK{Статус?}
    OK -->|200| PARSE["JSON parse"]
    OK -->|429/5xx| RETRY["Retry<br/>(exponential backoff)"]
    OK -->|4xx| ERROR["RuntimeError<br/>(нет retry)"]
    RETRY -->|"attempt < max"| REQ
    RETRY -->|"attempt >= max"| ERROR
    PARSE --> SUCCESS["Response dict"]
```
