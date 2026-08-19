"""Модуль анализа нагрузки контейнеров.

Формирует Prometheus-запросы для сбора метрик CPU, RAM и working-set
контейнеров и нод, собирает результаты в единый список ContainerStat
для каждого анализируемого периода.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

from app.analyzer.analyzer import ContainerStat, Engine, MetricValue, instance_selector, round_value

logger = logging.getLogger("analyzer.container")


def container_selector(filters: dict[str, str], instances: Optional[list[str]] = None) -> str:
    """Собирает строку-селектор Prometheus для контейнеров.

    Объединяет переданные фильтры, исключает контейнеры с пустым
    именем и опционально фильтрует по списку инстансов.
    """
    parts = ['name!=""']
    for key in sorted(filters):
        parts.append(f'{key}="{filters[key]}"')
    parts.append(instance_selector(instances or []))
    return "{" + ",".join(parts) + "}"


def container_key_of(metric: dict[str, str], labels: Optional[list[str]] = None) -> str:
    """Возвращает уникальный ключ контейнера по набору меток.

    Ключ строится конкатенацией значений указанных лейблов через
    нулевой байт \\x00, что исключает коллизии.
    """
    if labels is None:
        labels = ["envir", "job", "instance", "name", "department"]
    return "\x00".join(metric.get(lbl, "") for lbl in labels)


def container_cpu_usage_query(sel: str, period: str, group_labels: str) -> str:
    """Формирует PromQL-запрос потребления CPU контейнерами.

    Вычисляет rate потребления секунд CPU за указанный период
    с группировкой по заданным лейблам.
    """
    return f"sum by ({group_labels}) (rate(container_cpu_usage_seconds_total{sel}[{period}]))"


def container_mem_ratio_query(sel: str, period: str) -> str:
    """Формирует PromQL-запрос отношения потребления RAM к лимиту.

    Использует средние значения за период без субзапроса,
    результат — процент от лимита памяти контейнера.
    """
    return (
        f"(avg_over_time(container_memory_working_set_bytes{sel}[{period}]) "
        f"/ avg_over_time(container_spec_memory_limit_bytes{sel}[{period}])) * 100"
    )


def container_ws_avg_query(sel: str, period: str) -> str:
    """Формирует PromQL-запрос среднего working-set памяти контейнера."""
    return f"avg_over_time(container_memory_working_set_bytes{sel}[{period}])"


def node_cores_query(inst_sel: str) -> str:
    """Формирует PromQL-запрос количества CPU-ядер ноды."""
    return f"count by (instance) (node_cpu_seconds_total{{{inst_sel}}})"


def node_mem_total_query(inst_sel: str) -> str:
    """Формирует PromQL-запрос общей оперативной памяти ноды."""
    return f"node_memory_MemTotal_bytes{{{inst_sel}}}"


def _index_container_by_key(series, labels: Optional[list[str]] = None) -> dict[str, float]:
    """Индексирует серию метрик по ключу контейнера."""
    return {container_key_of(s.metric, labels): s.value for s in series}


def _index_by_label(series, label: str) -> dict[str, float]:
    """Индексирует серию метрик по значению указанного лейбла."""
    return {s.metric.get(label): s.value for s in series}


def _get_or_create(
    stats: dict[str, ContainerStat], metric: dict[str, str], labels: Optional[list[str]] = None
) -> ContainerStat:
    """Возвращает существующий или создаёт новый ContainerStat по метке.

    Если для данного ключа контейнера запись уже есть — возвращает её,
    иначе создаёт новую запись с метаданными из metric и добавляет в словарь stats.
    """
    key = container_key_of(metric, labels)
    st = stats.get(key)
    if st is None:
        st = ContainerStat(
            name=metric.get("name", "").lstrip("/"),
            instance=metric.get("instance", ""),
            job=metric.get("job", ""),
            envir=metric.get("envir", ""),
            department=metric.get("department", ""),
        )
        stats[key] = st
    return st


async def run_containers(engine: Engine, instances: Optional[list[str]] = None) -> list[ContainerStat]:
    """Запускает полный анализ нагрузки контейнеров.

    Параллельно запрашивает лимиты CPU, количество ядер и объём RAM нод,
    затем для каждого анализируемого периода собирает метрики CPU, RAM
    и working-set. Возвращает отсортированный список ContainerStat
    с вычисленными значениями в процентах.
    """
    logger.info("Starting container analysis periods=%d periods=%s", len(engine.periods), engine.periods)

    group_labels = engine.containers_cfg.group_labels
    labels = [l.strip() for l in group_labels.split(",") if l.strip()]
    sel = container_selector(engine.containers_cfg.filters, instances)
    inst_sel = instance_selector(instances)

    limit_cores, node_cores, node_mem = await asyncio.gather(
        engine._q_cached(
            f"sum by ({group_labels}) ((container_spec_cpu_quota{sel} > 0) "
            f"/ (container_spec_cpu_period{sel} > 0))"
        ),
        engine._q_cached(node_cores_query(inst_sel)),
        engine._q_cached(node_mem_total_query(inst_sel)),
    )
    limit_by_key = _index_container_by_key(limit_cores or [], labels)
    cores_by_instance = _index_by_label(node_cores or [], "instance")
    mem_total_by_instance = _index_by_label(node_mem or [], "instance")

    stats: dict[str, ContainerStat] = {}

    for period in engine.periods:
        usage, mem_ratio, ws_avg = await asyncio.gather(
            engine._q(container_cpu_usage_query(sel, period, group_labels)),
            engine._q(container_mem_ratio_query(sel, period)),
            engine._q(container_ws_avg_query(sel, period)),
        )

        if usage is not None:
            for s in usage:
                if not math.isfinite(s.value):
                    continue
                st = _get_or_create(stats, s.metric, labels)

                limit = limit_by_key.get(container_key_of(s.metric, labels))
                if limit is not None and math.isfinite(limit) and limit > 0:
                    st.cpu.append(MetricValue(period=period, value=round_value(s.value / limit * 100, 1)))

                cores = cores_by_instance.get(s.metric.get("instance"))
                if cores is not None and math.isfinite(cores) and cores > 0:
                    st.cpu_vm.append(MetricValue(period=period, value=round_value(s.value / cores * 100, 1)))
        else:
            logger.warning("Container CPU usage query failed period=%s", period)

        if mem_ratio is not None:
            for s in mem_ratio:
                if not math.isfinite(s.value):
                    continue
                st = _get_or_create(stats, s.metric, labels)
                st.memory.append(MetricValue(period=period, value=round_value(s.value, 1)))
        else:
            logger.warning("Container memory ratio query failed period=%s", period)

        if ws_avg is not None:
            for s in ws_avg:
                if not math.isfinite(s.value):
                    continue
                node_total = mem_total_by_instance.get(s.metric.get("instance"))
                if node_total is None or not math.isfinite(node_total) or node_total <= 0:
                    continue
                st = _get_or_create(stats, s.metric, labels)
                st.mem_vm.append(MetricValue(period=period, value=round_value(s.value / node_total * 100, 1)))
        else:
            logger.warning("Container working set query failed period=%s", period)

    containers = sorted(stats.values(), key=lambda c: (c.instance, c.name))
    logger.info("Container analysis complete containers=%d", len(containers))
    return containers
