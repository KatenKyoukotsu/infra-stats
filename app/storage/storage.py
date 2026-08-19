"""Модуль хранения отчётов и уведомлений.

Обеспечивает долгосрочное хранение результатов анализа в SQLite
с ротацией по лимиту записей. Последний отчёт держится в памяти
для быстрого доступа (диффы, API-эндпоинты). Все тяжёлые операции
имеют async-обёртки для использования в event loop.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import sqlite3
from typing import Optional

from app.analyzer.analyzer import AnalysisReport, trim_report_containers
from app.config.config import ContainersConfig

logger = logging.getLogger("storage")

DEFAULT_MAX_REPORTS = 100
DEFAULT_MAX_NOTIFICATIONS = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  report TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(ts);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


class Storage:
    """Долгосрочное хранение отчётов и нотификаций в SQLite.

    Полный последний отчёт держится в памяти (для диффов и /api/containers);
    история живёт в SQLite-файле и ротируется по лимиту, чтобы не расти бесконечно.
    """

    def __init__(
        self,
        path: str = "data/infra_stats.db",
        max_reports: int = DEFAULT_MAX_REPORTS,
        max_notifications: int = DEFAULT_MAX_NOTIFICATIONS,
    ):
        """Инициализирует хранилище и создаёт таблицы при необходимости.

        Args:
            path: путь к файлу базы данных SQLite.
            max_reports: максимальное количество хранимых отчётов.
            max_notifications: максимальное количество хранимых уведомлений.
        """
        if max_reports <= 0:
            max_reports = DEFAULT_MAX_REPORTS
        if max_notifications <= 0:
            max_notifications = DEFAULT_MAX_NOTIFICATIONS
        self._path = str(path)
        self._max_reports = max_reports
        self._max_notifications = max_notifications
        self._last: Optional[AnalysisReport] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Создаёт соединение с SQLite и включает WAL-журналирование."""
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        """Создаёт каталог и таблицы, загружает последний отчёт в память."""
        parent = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT report FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            self._last = AnalysisReport.from_dict(json.loads(row["report"]))
        logger.debug("Storage initialized path=%s last_report=%s", self._path, self._last is not None)

    def add_report(self, report: AnalysisReport, containers_cfg: Optional[ContainersConfig] = None) -> None:
        """Сохраняет отчёт в базу и обновляет кэш последнего отчёта.

        Если передан containers_cfg, контейнеры в сохраняемом отчёте
        обрезаются до конфигурации (trim_report_containers). Старые
        отчёты удаляются при превышении лимита.
        """
        self._last = report
        trimmed = report if containers_cfg is None else trim_report_containers(report, containers_cfg)
        payload = json.dumps(trimmed.to_dict(), ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reports(ts, report) VALUES(?, ?)",
                (trimmed.timestamp.isoformat(), payload),
            )
            conn.execute(
                "DELETE FROM reports WHERE id NOT IN "
                "(SELECT id FROM reports ORDER BY id DESC LIMIT ?)",
                (self._max_reports,),
            )

    def get_last_report(self) -> tuple[Optional[AnalysisReport], bool]:
        """Возвращает последний отчёт из кэша.

        Returns:
            Кортеж (отчёт, True) или (None, False), если отчётов нет.
        """
        if self._last is None:
            return None, False
        return self._last, True

    def get_all_reports(self) -> list[AnalysisReport]:
        """Возвращает все сохранённые отчёты в хронологическом порядке."""
        with self._connect() as conn:
            rows = conn.execute("SELECT report FROM reports ORDER BY id").fetchall()
        return [AnalysisReport.from_dict(json.loads(r["report"])) for r in rows]

    def add_notification(self, payload: dict) -> None:
        """Сохраняет уведомление в базу и удаляет лишние при превышении лимита."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notifications(ts, payload) VALUES(?, ?)",
                (payload.get("timestamp", ""), json.dumps(payload, ensure_ascii=False)),
            )
            conn.execute(
                "DELETE FROM notifications WHERE id NOT IN "
                "(SELECT id FROM notifications ORDER BY id DESC LIMIT ?)",
                (self._max_notifications,),
            )

    def get_notifications(self) -> list[dict]:
        """Возвращает все сохранённые уведомления в хронологическом порядке."""
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM notifications ORDER BY id").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def clear(self) -> None:
        """Очищает все отчёты и уведомления, сбрасывает кэш последнего отчёта."""
        self._last = None
        with self._connect() as conn:
            conn.execute("DELETE FROM reports")
            conn.execute("DELETE FROM notifications")

    async def add_report_async(
        self, report: AnalysisReport, containers_cfg: Optional[ContainersConfig] = None
    ) -> None:
        """Асинхронная обёртка над add_report."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, functools.partial(self.add_report, report, containers_cfg))

    async def get_last_report_async(self) -> tuple[Optional[AnalysisReport], bool]:
        """Асинхронная обёртка над get_last_report."""
        return self.get_last_report()

    async def get_all_reports_async(self) -> list[AnalysisReport]:
        """Асинхронная обёртка над get_all_reports."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_all_reports)

    async def add_notification_async(self, payload: dict) -> None:
        """Асинхронная обёртка над add_notification."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, functools.partial(self.add_notification, payload))

    async def get_notifications_async(self) -> list[dict]:
        """Асинхронная обёртка над get_notifications."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_notifications)

    async def clear_async(self) -> None:
        """Асинхронная обёртка над clear."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.clear)
