"""Модуль анализа инфраструктурных метрик.

Содержит движок анализа (Engine), модели данных и вспомогательные функции
для сбора метрик CPU, памяти, дисков, OOM-событий и статуса эндпоинтов
из VictoriaMetrics / Prometheus, а также вычисления дельт между отчётами.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from cachetools import TTLCache

from app.config.config import AnalysisConfig, BlackboxConfig, ContainersConfig
from app.vmclient.vmclient import VmClient

logger = logging.getLogger("analyzer")

_CACHE_TTL = 300  # сек — кэш всех запросов к VM (защита от повторных /api/analyze)


def round_value(value: float, decimals: int) -> float:
    """Округление float аналогично Go: float64(int(v*pow+0.5))/pow."""
    pow_ = 10 ** decimals
    return float(int(value * pow_ + 0.5)) / pow_


def _finite(value: float) -> bool:
    return math.isfinite(value)


_RE_SPECIAL = re.compile(r"[.^$*+?()\[\]{}|\\]")


def _escape_regex(value: str) -> str:
    return _RE_SPECIAL.sub(lambda m: "\\" + m.group(0), value)


def instance_selector(instances: list[str]) -> str:
    """Формирует Prometheus-селектор instance для списка инстансов.

    Возвращает expression вида instance=~"a|b" или instance!="" если список пуст.
    """
    clean = [i for i in instances if i]
    if not clean:
        return 'instance!=""'
    return 'instance=~"' + "|".join(_escape_regex(i) for i in clean) + '"'


def mountpoint_selector(mountpoints: list[str]) -> str:
    """Формирует Prometheus-селектор mountpoint для точек монтирования.

    Возвращает expression вида mountpoint=~"^(/|/var/lib)$".
    Если список пуст, используется корневой раздел /.
    """
    mps = [m for m in mountpoints if m] or ["/"]
    return 'mountpoint=~"^(' + "|".join(_escape_regex(m) for m in mps) + ')$"'


_DISK_FSTYPE_FILTER = 'fstype!~"tmpfs|overlay|squashfs|ramfs|cgroup|devtmpfs"'


def _query_selector(selectors: list[str]) -> str:
    return ",".join(s for s in selectors if s)


def memory_query(selector: str, period: str) -> str:
    """Генерирует PromQL-запрос средней загрузки памяти за указанный период.

    Формула: (1 - MemAvailable / MemTotal) * 100, без субзапроса.
    """
    return (
        f"(1 - avg_over_time(node_memory_MemAvailable_bytes{{{selector}}}[{period}]) "
        f"/ avg_over_time(node_memory_MemTotal_bytes{{{selector}}}[{period}])) * 100"
    )


def disk_query(selector: str, mountpoints_sel: str, period: str) -> str:
    """Генерирует PromQL-запрос средней загрузки дисков за период.

    Включает фильтры по mountpoint и типу файловой системы (fstype).
    Результат — процент использования по каждому mountpoint.
    """
    sel = _query_selector([selector, mountpoints_sel, _DISK_FSTYPE_FILTER])
    return (
        f"avg by (instance, mountpoint) ((1 - avg_over_time("
        f"node_filesystem_avail_bytes{{{sel}}}[{period}]) "
        f"/ avg_over_time(node_filesystem_size_bytes{{{sel}}}[{period}])) * 100)"
    )


def _first_value(metrics: list[MetricValue]) -> float:
    if not metrics:
        return 0.0
    return metrics[0].value


def _first_diff(metrics: list[MetricValue]) -> Optional[float]:
    if not metrics or metrics[0].diff is None:
        return None
    return metrics[0].diff


def container_notable(cn: ContainerStat, cc: ContainersConfig) -> bool:
    """Проверяет, является ли контейнер значимым (notable) для включения в отчёт.

    Контейнер считается значимым, если любая из его метрик (CPU, память,
    cpu_vm, mem_vm) превышает high_threshold или дельта превышает change_threshold.
    """
    if _first_value(cn.cpu) >= cc.high_threshold:
        return True
    if _first_value(cn.memory) >= cc.high_threshold:
        return True
    if _first_value(cn.cpu_vm) >= cc.high_threshold:
        return True
    if _first_value(cn.mem_vm) >= cc.high_threshold:
        return True
    d = _first_diff(cn.cpu)
    if d is not None and abs(d) >= cc.change_threshold:
        return True
    d = _first_diff(cn.memory)
    if d is not None and abs(d) >= cc.change_threshold:
        return True
    return False


def container_hot(cn: ContainerStat, cc: ContainersConfig) -> bool:
    """Проверяет, является ли контейнер «горячим» (hot) по CPU или памяти.

    Контейнер считается горячим, если CPU >= cpu_threshold или память >= mem_threshold.
    """
    if _first_value(cn.cpu) >= cc.cpu_threshold:
        return True
    if _first_value(cn.memory) >= cc.mem_threshold:
        return True
    return False


def trim_report_containers(report: AnalysisReport, cc: ContainersConfig) -> AnalysisReport:
    """Возвращает копию отчёта, содержащую только значимые контейнеры.

    Используется для сокращения объёма данных в истории и API-ответах.
    """
    return AnalysisReport(
        timestamp=report.timestamp,
        targets=report.targets,
        containers=[c for c in report.containers if container_notable(c, cc)],
    )


@dataclass
class MetricValue:
    """Значение метрики для конкретного временного периода.

    Attributes:
        period: Временной период (например, "1h", "24h", "7d").
        value: Текущее значение метрики.
        diff: Разница с предыдущим отчётом (None если данных нет).
    """
    period: str
    value: float
    diff: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"period": self.period, "value": self.value}
        if self.diff is not None:
            d["diff"] = self.diff
        return d

    @classmethod
    def from_dict(cls, d: dict) -> MetricValue:
        return cls(period=str(d["period"]), value=float(d["value"]), diff=d.get("diff"))


@dataclass
class DiskStat:
    """Статистика диска для конкретной точки монтирования.

    Attributes:
        mountpoint: Путь точки монтирования (например, "/", "/var/lib").
        metrics: Список значений использования диска по периодам.
    """
    mountpoint: str
    metrics: list[MetricValue] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"mountpoint": self.mountpoint}
        if self.metrics:
            d["metrics"] = [m.to_dict() for m in self.metrics]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DiskStat:
        return cls(
            mountpoint=str(d["mountpoint"]),
            metrics=[MetricValue.from_dict(m) for m in d.get("metrics", [])],
        )


@dataclass
class OOMEvent:
    """Событие OOM-킬а (Out of Memory) за период.

    Attributes:
        period: Временной период наблюдения.
        count: Количество OOM-событий за период.
        diff: Разница количества с предыдущим отчётом.
    """
    period: str
    count: int
    diff: Optional[int] = None

    def to_dict(self) -> dict:
        d = {"period": self.period, "count": self.count}
        if self.diff is not None:
            d["diff"] = self.diff
        return d

    @classmethod
    def from_dict(cls, d: dict) -> OOMEvent:
        return cls(period=str(d["period"]), count=int(d["count"]), diff=d.get("diff"))


@dataclass
class EndpointStatus:
    """Статус доступности эндпоинта (Blackbox-мониторинг).

    Attributes:
        period: Временной период.
        status: Статус — "ok", "down" или "unmonitored".
        uptime: Процент аптайма за период.
        diff: Разница аптайма с предыдущим отчётом.
    """
    period: str
    status: str  # ok | down | unmonitored
    uptime: Optional[float] = None
    diff: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"period": self.period, "status": self.status}
        if self.uptime is not None:
            d["uptime"] = self.uptime
        if self.diff is not None:
            d["diff"] = self.diff
        return d

    @classmethod
    def from_dict(cls, d: dict) -> EndpointStatus:
        return cls(
            period=str(d["period"]),
            status=str(d["status"]),
            uptime=d.get("uptime"),
            diff=d.get("diff"),
        )


@dataclass
class TargetStats:
    """Сводная статистика по целевому серверу (инстансу).

    Attributes:
        name: Читаемое имя сервера.
        cpu: Загрузка CPU по периодам.
        memory: Загрузка памяти по периодам.
        disks: Статистика дисков по точкам монтирования.
        oom: События OOM-киллов по периодам.
        url: URL эндпоинта для Blackbox-проверки.
        endpoints: Статусы доступности эндпоинтов.
    """
    name: str
    cpu: list[MetricValue] = field(default_factory=list)
    memory: list[MetricValue] = field(default_factory=list)
    disks: list[DiskStat] = field(default_factory=list)
    oom: list[OOMEvent] = field(default_factory=list)
    url: str = ""
    endpoints: list[EndpointStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"name": self.name}
        if self.url:
            d["url"] = self.url
        if self.endpoints:
            d["endpoints"] = [e.to_dict() for e in self.endpoints]
        if self.cpu:
            d["cpu"] = [m.to_dict() for m in self.cpu]
        if self.memory:
            d["memory"] = [m.to_dict() for m in self.memory]
        if self.disks:
            d["disks"] = [ds.to_dict() for ds in self.disks]
        if self.oom:
            d["oom"] = [o.to_dict() for o in self.oom]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TargetStats:
        return cls(
            name=str(d["name"]),
            cpu=[MetricValue.from_dict(m) for m in d.get("cpu", [])],
            memory=[MetricValue.from_dict(m) for m in d.get("memory", [])],
            disks=[DiskStat.from_dict(ds) for ds in d.get("disks", [])],
            oom=[OOMEvent.from_dict(o) for o in d.get("oom", [])],
            url=str(d.get("url", "")),
            endpoints=[EndpointStatus.from_dict(e) for e in d.get("endpoints", [])],
        )

    def add_disk(self, mountpoint: str, metric: MetricValue) -> None:
        """Добавляет метрику диска, группируя по точке монтирования."""
        for d in self.disks:
            if d.mountpoint == mountpoint:
                d.metrics.append(metric)
                return
        self.disks.append(DiskStat(mountpoint=mountpoint, metrics=[metric]))


@dataclass
class ContainerStat:
    """Статистика одного контейнера (Docker / Kubernetes).

    Attributes:
        name: Имя контейнера.
        instance: Инстанс, на котором запущен контейнер.
        job: Prometheus job.
        envir: Окружение (например, "production", "staging").
        department: Отдел, ответственный за контейнер.
        cpu: Загрузка CPU контейнера по периодам.
        memory: Загрузка памяти контейнера по периодам.
        cpu_vm: Доля CPU на уровне виртуальной машины.
        mem_vm: Доля памяти на уровне виртуальной машины.
    """
    name: str
    instance: str
    job: str
    envir: str = ""
    department: str = ""
    cpu: list[MetricValue] = field(default_factory=list)
    memory: list[MetricValue] = field(default_factory=list)
    cpu_vm: list[MetricValue] = field(default_factory=list)
    mem_vm: list[MetricValue] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"name": self.name, "instance": self.instance, "job": self.job}
        if self.envir:
            d["envir"] = self.envir
        if self.department:
            d["department"] = self.department
        if self.cpu:
            d["cpu"] = [m.to_dict() for m in self.cpu]
        if self.memory:
            d["memory"] = [m.to_dict() for m in self.memory]
        if self.cpu_vm:
            d["cpu_vm"] = [m.to_dict() for m in self.cpu_vm]
        if self.mem_vm:
            d["mem_vm"] = [m.to_dict() for m in self.mem_vm]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ContainerStat:
        return cls(
            name=str(d["name"]),
            instance=str(d["instance"]),
            job=str(d["job"]),
            envir=str(d.get("envir", "")),
            department=str(d.get("department", "")),
            cpu=[MetricValue.from_dict(m) for m in d.get("cpu", [])],
            memory=[MetricValue.from_dict(m) for m in d.get("memory", [])],
            cpu_vm=[MetricValue.from_dict(m) for m in d.get("cpu_vm", [])],
            mem_vm=[MetricValue.from_dict(m) for m in d.get("mem_vm", [])],
        )


@dataclass
class AnalysisReport:
    """Полный отчёт анализа инфраструктуры.

    Attributes:
        timestamp: Время формирования отчёта.
        targets: Статистика по целевым серверам.
        containers: Статистика по контейнерам (если включено).
    """
    timestamp: datetime
    targets: list[TargetStats] = field(default_factory=list)
    containers: list[ContainerStat] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"timestamp": self.timestamp.isoformat(), "targets": [t.to_dict() for t in self.targets]}
        if self.containers:
            d["containers"] = [c.to_dict() for c in self.containers]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> AnalysisReport:
        return cls(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            targets=[TargetStats.from_dict(t) for t in d.get("targets", [])],
            containers=[ContainerStat.from_dict(c) for c in d.get("containers", [])],
        )


@dataclass
class TargetInput:
    """Входные параметры целевого сервера для анализа.

    Attributes:
        name: Читаемое имя сервера.
        instance: Идентификатор инстанса в Prometheus (например, "10.0.0.1:9100").
        mountpoints: Список точек монтирования для мониторинга дисков.
        url: URL для Blackbox-проверки доступности.
    """
    name: str
    instance: str
    mountpoints: list[str] = field(default_factory=lambda: ["/"])
    url: str = ""


class Engine:
    """Движок анализа инфраструктурных метрик.

    Координирует параллельный сбор CPU, памяти, дисков, OOM-событий
    и статуса эндпоинтов из VictoriaMetrics/Prometheus.
    Использует TTL-кэш для защиты от повторных запросов.

    Attributes:
        client: Клиент для выполнения запросов к VM/Prometheus.
        cpu: Конфигурация анализа CPU.
        memory: Конфигурация анализа памяти.
        disk: Конфигурация анализа дисков.
        oom: Конфигурация анализа OOM-событий.
        periods: Список временных периодов для анализа.
        containers_cfg: Конфигурация анализа контейнеров.
        containers_on: Флаг включения анализа контейнеров.
        endpoints_cfg: Конфигурация Blackbox-мониторинга.
    """
    def __init__(
        self,
        client: VmClient,
        analysis: AnalysisConfig,
        containers: ContainersConfig,
        blackbox: Optional[BlackboxConfig] = None,
        cache_ttl: int = 300,
        cache_maxsize: int = 2000,
    ):
        self.client = client
        self.cpu = analysis.cpu
        self.memory = analysis.memory
        self.disk = analysis.disk
        self.oom = analysis.oom
        self.periods = list(analysis.periods)
        self.containers_cfg = containers
        self.containers_on = containers.enabled
        self.endpoints_cfg = blackbox or BlackboxConfig()
        self._cache: TTLCache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)

        logger.debug(
            "Creating analyzer engine cpu=%s memory=%s disk=%s oom=%s periods=%s containers=%s blackbox_job=%s cache_ttl=%d cache_maxsize=%d",
            self.cpu,
            self.memory,
            self.disk,
            self.oom,
            self.periods,
            containers.enabled,
            self.endpoints_cfg.job,
            cache_ttl,
            cache_maxsize,
        )

    def containers_enabled(self) -> bool:
        """Возвращает True, если анализ контейнеров включён."""
        return self.containers_on

    async def run_containers(self, instances: Optional[list[str]] = None) -> list[ContainerStat]:
        """Запускает анализ метрик контейнеров для указанных инстансов."""
        from app.analyzer.container import run_containers

        return await run_containers(self, instances)

    async def run_analysis(self, targets: list[TargetInput]) -> AnalysisReport:
        """Выполняет полный анализ метрик по списку целей.

        Запускает параллельные циклы сбора CPU, памяти, дисков, OOM и
        эндпоинтов, затем возвращает заполненный отчёт.
        """
        logger.info("Starting metrics analysis targets=%d", len(targets))

        report = AnalysisReport(
            timestamp=datetime.now(),
            targets=[TargetStats(name=t.name, url=t.url) for t in targets],
        )
        idx_by_instance = {t.instance: i for i, t in enumerate(targets)}
        idx_by_url = {t.url: i for i, t in enumerate(targets) if t.url}
        selector = instance_selector([t.instance for t in targets])

        loops = []
        if self.cpu:
            loops.append(self._loop_cpu(report.targets, idx_by_instance, selector))
        if self.memory:
            loops.append(self._loop_memory(report.targets, idx_by_instance, selector))
        if self.disk:
            loops.append(self._loop_disk(report.targets, idx_by_instance, targets, selector))
        if self.oom:
            loops.append(self._loop_oom(report.targets, idx_by_instance, selector))
        if idx_by_url:
            loops.append(self._loop_endpoints(report.targets, idx_by_url))
        if loops:
            await asyncio.gather(*loops)

        logger.info("Analysis complete targets=%d timestamp=%s", len(report.targets), report.timestamp)
        return report

    async def _q(self, query: str, ttl: float = _CACHE_TTL):
        hit = self._cache.get(query)
        if hit is not None:
            return hit
        try:
            result = await self.client.query(query)
        except Exception as exc:
            logger.warning("Query failed error=%s query=%s", exc, query)
            return None
        if result is not None:
            self._cache[query] = result
        return result

    async def _q_cached(self, query: str, ttl: float = _CACHE_TTL):
        return await self._q(query, ttl)

    async def _loop_cpu(self, targets, idx_by_instance, selector) -> None:
        for period in self.periods:
            query = f'100 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle",{selector}}}[{period}])) * 100'
            series = await self._q(query)
            if series is None:
                logger.warning("CPU query failed period=%s", period)
                continue
            for s in series:
                idx = idx_by_instance.get(s.metric.get("instance"))
                if idx is None or not _finite(s.value):
                    continue
                targets[idx].cpu.append(MetricValue(period=period, value=round_value(s.value, 1)))

    async def _loop_memory(self, targets, idx_by_instance, selector) -> None:
        for period in self.periods:
            query = memory_query(selector, period)
            series = await self._q(query)
            if series is None:
                logger.warning("Memory query failed period=%s", period)
                continue
            for s in series:
                idx = idx_by_instance.get(s.metric.get("instance"))
                if idx is None or not _finite(s.value):
                    continue
                targets[idx].memory.append(MetricValue(period=period, value=round_value(s.value, 1)))

    async def _loop_disk(self, targets, idx_by_instance, targets_cfg, selector) -> None:
        mountpoints: list[str] = []
        for t in targets_cfg:
            mountpoints.extend(t.mountpoints or [])
        mp_sel = mountpoint_selector(mountpoints)
        allowed = {t.instance: set(t.mountpoints) for t in targets_cfg}

        for period in self.periods:
            query = disk_query(selector, mp_sel, period)
            series = await self._q(query)
            if series is None:
                logger.warning("Disk query failed period=%s", period)
                continue

            for s in series:
                idx = idx_by_instance.get(s.metric.get("instance"))
                if idx is None or not _finite(s.value):
                    continue
                mp = s.metric.get("mountpoint")
                if mp not in allowed.get(s.metric.get("instance"), set()):
                    continue
                targets[idx].add_disk(mp, MetricValue(period=period, value=round_value(s.value, 1)))

    async def _loop_oom(self, targets, idx_by_instance, selector) -> None:
        for period in self.periods:
            query = f"sum by (instance) (increase(node_vmstat_oom_kill{{{selector}}}[{period}]))"
            series = await self._q(query)
            if series is None:
                logger.debug("OOM query returned no data period=%s", period)
                continue
            for s in series:
                idx = idx_by_instance.get(s.metric.get("instance"))
                if idx is None or not _finite(s.value):
                    continue
                count = int(s.value)
                if count <= 0:
                    continue
                targets[idx].oom.append(OOMEvent(period=period, count=count))

    async def _loop_endpoints(self, targets, idx_by_url) -> None:
        from app.analyzer.blackbox import (
            STATUS_UNMONITORED,
            _index_endpoint_by_url,
            _normalize,
            _status_for,
            endpoint_uptime_query,
        )

        job = self.endpoints_cfg.job
        ok_threshold = self.endpoints_cfg.ok_threshold
        for period in self.periods:
            query = endpoint_uptime_query(job, period)
            series = await self._q(query)
            if series is None:
                logger.warning("Blackbox query failed period=%s", period)
                continue
            idx = _index_endpoint_by_url(series)
            for url, i in idx_by_url.items():
                value = idx.get(_normalize(url))
                if value is None or not _finite(value):
                    targets[i].endpoints.append(EndpointStatus(period=period, status=STATUS_UNMONITORED))
                    continue
                uptime = round_value(max(value, 0.0), 1)
                targets[i].endpoints.append(
                    EndpointStatus(period=period, status=_status_for(uptime, ok_threshold), uptime=uptime)
                )


def compute_diffs(current: AnalysisReport, previous: AnalysisReport) -> AnalysisReport:
    """Вычисляет разницу (дельту) между текущим и предыдущим отчётами.

    Заполняет поле diff у метрик, OOM-событий и эндпоинтов в текущем отчёте.
    Если предыдущий отчёт пуст, текущий возвращается без изменений.
    """
    if not previous.targets:
        return current

    prev_by_target = {t.name: t for t in previous.targets}
    for target in current.targets:
        prev = prev_by_target.get(target.name)
        if prev is None:
            continue
        for m in target.cpu:
            m.diff = _metric_diff(m, prev.cpu)
        for m in target.memory:
            m.diff = _metric_diff(m, prev.memory)
        for d in target.disks:
            prev_disk = next((x for x in prev.disks if x.mountpoint == d.mountpoint), None)
            if prev_disk is not None:
                for m in d.metrics:
                    m.diff = _metric_diff(m, prev_disk.metrics)
        for o in target.oom:
            o.diff = _oom_diff(o, prev.oom)
        prev_endpoints = {s.period: s for s in prev.endpoints}
        for st in target.endpoints:
            p = prev_endpoints.get(st.period)
            if p is None or p.uptime is None or st.uptime is None:
                continue
            st.diff = round_value(st.uptime - p.uptime, 1)

    prev_by_container = {_container_key(c): c for c in previous.containers}
    for cur in current.containers:
        prev = prev_by_container.get(_container_key(cur))
        if prev is None:
            continue
        for m in cur.cpu:
            m.diff = _metric_diff(m, prev.cpu)
        for m in cur.memory:
            m.diff = _metric_diff(m, prev.memory)
        for m in cur.cpu_vm:
            m.diff = _metric_diff(m, prev.cpu_vm)
        for m in cur.mem_vm:
            m.diff = _metric_diff(m, prev.mem_vm)

    return current


def _container_key(c: ContainerStat) -> str:
    return c.instance + "\x00" + c.name


def _metric_diff(curr: MetricValue, prev_metrics: list[MetricValue]) -> Optional[float]:
    for p in prev_metrics:
        if p.period == curr.period:
            return round_value(curr.value - p.value, 1)
    return None


def _oom_diff(curr: OOMEvent, prev_events: list[OOMEvent]) -> Optional[int]:
    for p in prev_events:
        if p.period == curr.period:
            return curr.count - p.count
    return None
