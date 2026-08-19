"""Модуль анализа доступности эндпоинтов (Blackbox Exporter).

Построение PromQL-запросов для вычисления uptime эндпоинтов
и определение их статуса (ok / down / unmonitored) по порогу
успешных проверок.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("analyzer.blackbox")

STATUS_OK = "ok"
STATUS_DOWN = "down"
STATUS_UNMONITORED = "unmonitored"

DEFAULT_OK_THRESHOLD = 99.99


def endpoint_uptime_query(job: str, period: str) -> str:
    """Формирует PromQL-запрос uptime эндпоинта.

    Вычисляет среднее значение probe_success за указанный период,
    результат — процент успешных проверок.
    """
    return f'avg_over_time(probe_success{{job="{job}"}}[{period}]) * 100'


def endpoint_url_of(metric: dict[str, str]) -> str:
    """Извлекает URL эндпоинта из меток серии.

    Использует лейбл target (стандартный relabel Blackbox Exporter)
    или instance, если target отсутствует.
    """
    return (metric.get("target") or metric.get("instance") or "").rstrip("/")


def _normalize(url: str) -> str:
    """Нормализует URL, убирая завершающий слэш."""
    return (url or "").rstrip("/")


def _status_for(uptime: float, ok_threshold: float = DEFAULT_OK_THRESHOLD) -> str:
    """Определяет статус эндпоинта по значению uptime.

    Возвращает STATUS_OK, если uptime >= порога, иначе STATUS_DOWN.
    """
    if uptime >= ok_threshold:
        return STATUS_OK
    return STATUS_DOWN


def _index_endpoint_by_url(series) -> dict[str, float]:
    """Индексирует серию метрик uptime по URL эндпоинта."""
    idx: dict[str, float] = {}
    for s in series:
        url = endpoint_url_of(s.metric)
        if url:
            idx[url] = s.value
    return idx
