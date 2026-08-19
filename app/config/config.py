"""Модуль конфигурации сервиса infra-stats.

Загружает YAML-конфиг, парсит его в dataclass-ы и применяет
env-переменные поверх файловых значений (env > file > default).

Иерархия конфигурации::

    config.yaml  →  load_config()  →  Config
    ENV vars     →  apply on top   →  Config (with overrides)

Пример использования::

    from app.config.config import load_config
    cfg = load_config("configs/config.yaml")
    print(cfg.victoria_metrics.url)
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

# ──────────────────────────────────────────────────────────────────
# Defaults — единый источник правды для всех дефолтных значений.
# Ключевые параметры вынесены сюда, чтобы не дублировать в коде.
# ──────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_CONCURRENT = 8
DEFAULT_RPS = 20.0
DEFAULT_RETRIES = 3
DEFAULT_JITTER = 30.0
DEFAULT_PERIODS = ["1d", "7d", "14d"]
DEFAULT_STORAGE_PATH = "data/infra_stats.db"
DEFAULT_MAX_REPORTS = 100
DEFAULT_MAX_NOTIFICATIONS = 50
DEFAULT_ANALYZE_TIMEOUT = 300  # сек
DEFAULT_OK_THRESHOLD = 99.99
DEFAULT_BASE_BACKOFF = 0.3
DEFAULT_MAX_BACKOFF = 5.0
DEFAULT_CACHE_TTL = 300  # сек
DEFAULT_CACHE_MAXSIZE = 2000
DEFAULT_CONTAINER_GROUP_LABELS = "envir, job, instance, name, department"


# ──────────────────────────────────────────────────────────────────
# Хелперы для безопасного парсинга значений.
# Согласованы с Go zero-value: 0/None/pустая строка → default.
# ──────────────────────────────────────────────────────────────────

def _num(value: Any, default: float) -> float:
    """Приводит значение к float; 0/None/"" считаются не заданными."""
    if value in (None, 0, ""):
        return default
    return float(value)


def _int(value: Any, default: int) -> int:
    """Приводит значение к int; 0/None/"" считаются не заданными."""
    if value in (None, 0, ""):
        return default
    return int(value)


_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _duration(value: Any, default: float) -> float:
    """Парсит длительность в Go-стиле ("30s", "1m", "500ms") в секунды.

    Принимает строки, числа или None. Если значение не парсится — возвращает default.
    """
    if value in (None, 0, ""):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for suffix, mult in _UNITS.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * mult
            except ValueError:
                break
    try:
        return float(text)
    except ValueError:
        return default


# ──────────────────────────────────────────────────────────────────
# Dataclass-ы конфигурации.
# Каждый раздел YAML маппится на свой dataclass.
# ──────────────────────────────────────────────────────────────────

@dataclass
class VMConfig:
    """Параметры подключения к VictoriaMetrics.

    Attributes:
        url: Базовый URL API VictoriaMetrics (например, http://vm:8428).
        timeout: Таймаут HTTP-запросов в секундах.
        max_concurrent: Максимальное количество параллельных запросов (semaphore).
        rps: Лимит запросов в секунду (token bucket rate).
        retries: Количество повторных попыток при ошибках.
        base_backoff: Начальный бэкoff перед retry (сек).
        max_backoff: Максимальный бэкoff (сек).
    """
    url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    rps: float = DEFAULT_RPS
    retries: int = DEFAULT_RETRIES
    base_backoff: float = DEFAULT_BASE_BACKOFF
    max_backoff: float = DEFAULT_MAX_BACKOFF


@dataclass
class TargetConfig:
    """Описание целевого хоста (ВМ) для анализа.

    Attributes:
        name: Человекочитаемое имя хоста.
        instance: Метка ``instance`` в VictoriaMetrics (например, "10.0.0.1:9100").
        mountpoints: Список точек монтирования для анализа дисков (по умолчанию ["/"]).
        description: Описание хоста.
        url: URL эндпоинта для blackbox-проверок (опционально).
    """
    name: str = ""
    instance: str = ""
    mountpoints: list[str] = field(default_factory=list)
    description: str = ""
    url: str = ""


@dataclass
class AnalysisConfig:
    """Какие метрики собирать и за какие периоды.

    Attributes:
        cpu: Собирать метрики CPU.
        memory: Собирать метрики памяти.
        disk: Собирать метрики дисков.
        oom: Собирать метрики OOM-kill.
        periods: Временные окна для анализа (например, ["1d", "7d", "14d"]).
    """
    cpu: bool = True
    memory: bool = True
    disk: bool = True
    oom: bool = True
    periods: list[str] = field(default_factory=lambda: list(DEFAULT_PERIODS))


@dataclass
class ContainersConfig:
    """Настройки анализа Docker/Kubernetes-контейнеров.

    Attributes:
        enabled: Включить анализ контейнеров.
        change_threshold: Порог изменения для notable-контейнеров (%).
        high_threshold: Порог «высокого потребления» для notable (%).
        cpu_threshold: Порог CPU для hot-контейнеров (%).
        mem_threshold: Порог памяти для hot-контейнеров (%).
        filters: Фильтры по лейблам контейнеров ({"envir": "prod"}).
        group_labels: Строка лейблов для group by в PromQL-запросах.
    """
    enabled: bool = False
    change_threshold: float = 5.0
    high_threshold: float = 70.0
    cpu_threshold: float = 80.0
    mem_threshold: float = 95.0
    filters: dict[str, str] = field(default_factory=dict)
    group_labels: str = DEFAULT_CONTAINER_GROUP_LABELS


@dataclass
class BlackboxConfig:
    """Настройки blackbox-проверок доступности эндпоинтов.

    Attributes:
        job: Имя job в VictoriaMetrics для фильтрации серий blackbox.
        ok_threshold: Минимальный uptime (%) для статуса "ok" (по умолчанию 99.99).
    """
    job: str = "blackbox"
    ok_threshold: float = DEFAULT_OK_THRESHOLD


@dataclass
class MessengerConfig:
    """Параметры подключения к мессенджеру для уведомлений.

    Attributes:
        enabled: Включить уведомления.
        api_url: URL API мессенджера.
        chat_id: ID чата для отправки уведомлений.
        bearer_token: Bearer-токен для авторизации (задаётся через env).
    """
    enabled: bool = False
    api_url: str = ""
    chat_id: str = ""
    bearer_token: str = ""


@dataclass
class NotifierConfig:
    """Конфигурация нотификаций (пока только BotX/Clouds)."""
    botx: MessengerConfig = field(default_factory=MessengerConfig)


@dataclass
class SchedulerConfig:
    """Параметры планировщика задач.

    Attributes:
        analyze_cron: Cron-выражение для запуска анализа.
        send_cron: Cron-выражение для отправки отчёта.
        jitter: Максимальный jitter перед выполнением (сек).
        analyze_timeout: Таймаут одного анализа (сек).
    """
    analyze_cron: str = "0 0 * * *"
    send_cron: str = "0 8 * * *"
    jitter: float = DEFAULT_JITTER
    analyze_timeout: int = DEFAULT_ANALYZE_TIMEOUT


@dataclass
class StorageConfig:
    """Параметры хранилища отчётов (SQLite).

    Attributes:
        path: Путь к файлу базы данных.
        max_reports: Максимальное количество отчётов в истории.
        max_notifications: Максимальное количество уведомлений в истории.
    """
    path: str = DEFAULT_STORAGE_PATH
    max_reports: int = DEFAULT_MAX_REPORTS
    max_notifications: int = DEFAULT_MAX_NOTIFICATIONS


@dataclass
class Config:
    """Корневая конфигурация — собирает все разделы в один объект.

    Attributes:
        victoria_metrics: Параметры подключения к VM.
        targets: Список целевых хостов для анализа.
        analysis: Какие метрики и за какие периоды собирать.
        containers: Настройки анализа контейнеров.
        blackbox: Настройки blackbox-проверок.
        scheduler: Параметры планировщика.
        notifier: Настройки уведомлений.
        storage: Параметры хранилища.
        api_key: API-ключ для защиты write-эндпоинтов (задаётся через env).
        cache_ttl: Время жизни кэша запросов к VM (сек).
        cache_maxsize: Максимальное количество записей в кэше.
    """
    victoria_metrics: VMConfig = field(default_factory=VMConfig)
    targets: list[TargetConfig] = field(default_factory=list)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    containers: ContainersConfig = field(default_factory=ContainersConfig)
    blackbox: BlackboxConfig = field(default_factory=BlackboxConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    api_key: str = ""
    cache_ttl: int = DEFAULT_CACHE_TTL
    cache_maxsize: int = DEFAULT_CACHE_MAXSIZE

    def to_dict(self) -> dict:
        """Сериализует конфигуцию в dict (для API /api/config)."""
        return asdict(self)


# ──────────────────────────────────────────────────────────────────
# Парсеры YAML → dataclass.
# Каждый _parse_* отвечает за один раздел конфига.
# ──────────────────────────────────────────────────────────────────

def _parse_targets(raw: Any) -> list[TargetConfig]:
    """Парсит список targets из YAML."""
    targets: list[TargetConfig] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        targets.append(
            TargetConfig(
                name=str(item.get("name", "")),
                instance=str(item.get("instance", "")),
                mountpoints=list(item.get("mountpoints") or []),
                description=str(item.get("description", "")),
                url=str(item.get("url", "")),
            )
        )
    return targets


def _parse_analysis(raw: Any) -> AnalysisConfig:
    """Парсит раздел analysis. Если ни один тип не указан — включает все."""
    raw = raw or {}
    cpu = bool(raw.get("cpu", False))
    memory = bool(raw.get("memory", False))
    disk = bool(raw.get("disk", False))
    oom = bool(raw.get("oom", False))
    if not (cpu or memory or disk or oom):
        cpu = memory = disk = oom = True
    periods = list(raw.get("periods") or DEFAULT_PERIODS)
    return AnalysisConfig(cpu=cpu, memory=memory, disk=disk, oom=oom, periods=periods)


def _parse_containers(raw: Any) -> ContainersConfig:
    """Парсит раздел containers."""
    raw = raw or {}
    filters = dict(raw.get("filters") or {})
    return ContainersConfig(
        enabled=bool(raw.get("enabled", False)),
        change_threshold=_num(raw.get("change_threshold"), 5.0),
        high_threshold=_num(raw.get("high_threshold"), 70.0),
        cpu_threshold=_num(raw.get("cpu_threshold"), 80.0),
        mem_threshold=_num(raw.get("mem_threshold"), 95.0),
        filters=filters,
        group_labels=str(raw.get("group_labels") or DEFAULT_CONTAINER_GROUP_LABELS),
    )


def _parse_blackbox(raw: Any) -> BlackboxConfig:
    """Парсит раздел blackbox."""
    raw = raw or {}
    return BlackboxConfig(
        job=str(raw.get("job") or "blackbox"),
        ok_threshold=_num(raw.get("ok_threshold"), DEFAULT_OK_THRESHOLD),
    )


def _parse_scheduler(raw: Any) -> SchedulerConfig:
    """Парсит раздел scheduler."""
    raw = raw or {}
    return SchedulerConfig(
        analyze_cron=str(raw.get("analyze_cron", "0 0 * * *")),
        send_cron=str(raw.get("send_cron", "0 8 * * *")),
        jitter=_duration(raw.get("jitter"), DEFAULT_JITTER),
        analyze_timeout=_int(raw.get("analyze_timeout"), DEFAULT_ANALYZE_TIMEOUT),
    )


def _parse_notifier(raw: Any) -> NotifierConfig:
    """Парсит раздел notifier."""
    botx = (raw or {}).get("botx") or {}
    return NotifierConfig(
        botx=MessengerConfig(
            enabled=bool(botx.get("enabled", False)),
            api_url=str(botx.get("api_url", "")),
            chat_id=str(botx.get("chat_id", "")),
            bearer_token=str(botx.get("bearer_token", "")),
        )
    )


def _parse_storage(raw: Any) -> StorageConfig:
    """Парсит раздел storage."""
    raw = raw or {}
    return StorageConfig(
        path=str(raw.get("path") or DEFAULT_STORAGE_PATH),
        max_reports=_int(raw.get("max_reports"), DEFAULT_MAX_REPORTS),
        max_notifications=_int(raw.get("max_notifications"), DEFAULT_MAX_NOTIFICATIONS),
    )


def load_config(path: str) -> Config:
    """Загружает конфигуцию из YAML-файла и применяет env-overrides.

    Приоритет: ENV > YAML > default.

    Поддерживаемые env-переменные:
        - ``BOTX_BEARER_TOKEN`` → ``notifier.botx.bearer_token``
        - ``BOTX_CHAT_ID`` → ``notifier.botx.chat_id``
        - ``STORAGE_PATH`` → ``storage.path``
        - ``API_KEY`` → ``api_key``

    Args:
        path: Путь к YAML-файлу конфигурации.

    Returns:
        Полностью сконфигурированный объект Config.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    vm_raw = raw.get("victoria_metrics") or {}
    cfg = Config(
        victoria_metrics=VMConfig(
            url=str(vm_raw.get("url", "")),
            timeout=_duration(vm_raw.get("timeout"), DEFAULT_TIMEOUT),
            max_concurrent=_int(vm_raw.get("max_concurrent"), DEFAULT_MAX_CONCURRENT),
            rps=_num(vm_raw.get("rps"), DEFAULT_RPS),
            retries=_int(vm_raw.get("retries"), DEFAULT_RETRIES),
            base_backoff=_num(vm_raw.get("base_backoff"), DEFAULT_BASE_BACKOFF),
            max_backoff=_num(vm_raw.get("max_backoff"), DEFAULT_MAX_BACKOFF),
        ),
        targets=_parse_targets(raw.get("targets")),
        analysis=_parse_analysis(raw.get("analysis")),
        containers=_parse_containers(raw.get("containers")),
        blackbox=_parse_blackbox(raw.get("blackbox")),
        scheduler=_parse_scheduler(raw.get("scheduler")),
        notifier=_parse_notifier(raw.get("notifier")),
        storage=_parse_storage(raw.get("storage")),
        api_key=str((raw.get("security") or {}).get("api_key", "")),
        cache_ttl=_int((raw.get("cache") or {}).get("ttl"), DEFAULT_CACHE_TTL),
        cache_maxsize=_int((raw.get("cache") or {}).get("maxsize"), DEFAULT_CACHE_MAXSIZE),
    )

    # Env-overrides: секреты и параметры окружения
    if env_token := os.environ.get("BOTX_BEARER_TOKEN"):
        cfg.notifier.botx.bearer_token = env_token
    if env_chat_id := os.environ.get("BOTX_CHAT_ID"):
        cfg.notifier.botx.chat_id = env_chat_id
    if env_storage_path := os.environ.get("STORAGE_PATH"):
        cfg.storage.path = env_storage_path
    if env_api_key := os.environ.get("API_KEY"):
        cfg.api_key = env_api_key

    return cfg
