"""Модуль планировщика задач.

Управляет расписанием анализа метрик инфраструктуры и отправки отчётов
в мессенджер Clouds (BotX). Обеспечивает запуск задач по cron-расписанию,
отслеживание их статуса и передачу результатов в хранилище и нотификатор.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.analyzer.analyzer import AnalysisReport, Engine, TargetInput, compute_diffs
from app.config.config import DEFAULT_ANALYZE_TIMEOUT
from app.config.manager import Manager
from app.notifier.notifier import Client as NotifierClient
from app.storage.storage import Storage

logger = logging.getLogger("scheduler")


@dataclass
class JobStatus:
    """Статус выполнения отдельной задачи планировщика.

    Хранит информацию о времени последнего запуска,
    успешности завершения и тексте ошибки (если была).
    """

    last_run: Optional[datetime] = None
    last_success: bool = False
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        """Преобразует статус задачи в словарь для сериализации."""
        d = {
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_success": self.last_success,
        }
        if self.last_error:
            d["last_error"] = self.last_error
        return d


@dataclass
class Status:
    """Агрегированный статус обоих задач планировщика: анализа и отправки."""

    analyze: JobStatus = field(default_factory=JobStatus)
    send: JobStatus = field(default_factory=JobStatus)

    def to_dict(self) -> dict:
        """Преобразует общий статус в словарь для сериализации."""
        return {"analyze": self.analyze.to_dict(), "send": self.send.to_dict()}


class Scheduler:
    """Планировщик задач, координирующий анализ метрик и отправку отчётов.

    Использует APScheduler для cron-управления двумя фоновыми задачами:
    анализом инфраструктуры (``analyze``) и доставкой отчёта (``send``).
    """

    def __init__(self, cfg_mgr: Manager, engine: Engine, store: Storage, notifier: NotifierClient):
        """Инициализирует планировщик с необходимыми зависимостями.

        Args:
            cfg_mgr: Менеджер конфигурации.
            engine: Движок анализа метрик.
            store: Хранилище отчётов.
            notifier: Клиент отправки уведомлений.
        """
        self.cfg_mgr = cfg_mgr
        self.engine = engine
        self.store = store
        self.notifier = notifier
        self.status = Status()
        self._sched = AsyncIOScheduler()

    def status_dict(self) -> dict:
        """Возвращает текущий статус всех задач в виде словаря."""
        return self.status.to_dict()

    async def analyze_now(self) -> AnalysisReport:
        """Выполняет анализ метрик немедленно по текущей конфигурации.

        Запускает анализ по указанным targets, вычисляет дельту
        по сравнению с предыдущим отчётом и сохраняет результат.

        Returns:
            Отчёт анализа с данными по каждому целевому серверу.
        """
        cfg = self.cfg_mgr.get()
        targets = build_target_inputs(cfg.targets)

        async with asyncio.timeout(cfg.scheduler.analyze_timeout):
            report = await self.engine.run_analysis(targets)
            if self.engine.containers_enabled():
                report.containers = await self.engine.run_containers(instances=[t.instance for t in targets])

        if prev := self.store.get_last_report()[0]:
            report = compute_diffs(report, prev)
        self.store.add_report(report, cfg.containers)
        return report

    def start(self) -> None:
        """Запускает планировщик, регистрируя cron-задачи анализа и отправки."""
        cfg = self.cfg_mgr.get()
        self._sched.add_job(
            self._job_analyze,
            CronTrigger.from_crontab(cfg.scheduler.analyze_cron),
            id="analyze",
            max_instances=1,
            coalesce=True,
        )
        self._sched.add_job(
            self._job_send,
            CronTrigger.from_crontab(cfg.scheduler.send_cron),
            id="send",
            max_instances=1,
            coalesce=True,
        )
        self._sched.start()
        logger.info(
            "Scheduler started analyze_cron=%s send_cron=%s jitter=%ss",
            cfg.scheduler.analyze_cron,
            cfg.scheduler.send_cron,
            cfg.scheduler.jitter,
        )

    def stop(self) -> None:
        """Останавливает планировщик без ожидания завершения текущих задач."""
        logger.info("Stopping scheduler...")
        self._sched.shutdown(wait=False)

    async def _job_analyze(self) -> None:
        """Целевая функция cron-задачи анализа метрик."""
        logger.info("[CRON] Scheduled metrics analysis started")
        await apply_jitter(self.cfg_mgr.get().scheduler.jitter)
        start = datetime.now()
        try:
            report = await self.analyze_now()
            self.status.analyze = JobStatus(last_run=start, last_success=True)
            logger.info("[CRON] Scheduled analysis finished targets=%d", len(report.targets))
        except Exception as exc:
            self.status.analyze = JobStatus(last_run=start, last_success=False, last_error=str(exc))
            logger.error("[CRON] Scheduled analysis failed error=%s", exc)

    async def _job_send(self) -> None:
        """Целевая функция cron-задачи отправки отчёта в BotX."""
        logger.info("[CRON] Scheduled BotX report trigger executed")
        start = datetime.now()
        try:
            last_report, ok = self.store.get_last_report()
            if not ok:
                self.status.send = JobStatus(
                    last_run=start, last_success=False, last_error="no reports available"
                )
                logger.warning("[CRON] Skipping notification: no reports available in storage")
                return
            await self.notifier.send_report(self.cfg_mgr.get(), last_report)
            self.status.send = JobStatus(last_run=start, last_success=True)
            logger.info("[CRON] Scheduled report successfully delivered to BotX")
        except Exception as exc:
            self.status.send = JobStatus(last_run=start, last_success=False, last_error=str(exc))
            logger.error("[CRON] Failed to send scheduled report to BotX error=%s", exc)


async def apply_jitter(max_jitter: float) -> None:
    """Применяет случайную задержку перед выполнением задачи для распределения нагрузки.

    Args:
        max_jitter: Максимальная величина задержки в секундах.
    """
    if max_jitter <= 0:
        return
    delay = random.random() * max_jitter
    if delay > 0:
        logger.info("Applying scheduler jitter delay=%ss", round(delay, 3))
        await asyncio.sleep(delay)


def build_target_inputs(cfg_targets) -> list[TargetInput]:
    """Преобразует список целей из конфигурации в объекты TargetInput.

    Args:
        cfg_targets: Список целевых серверов из конфигурации.

    Returns:
        Список TargetInput, готовых для передачи в движок анализа.
    """
    targets: list[TargetInput] = []
    for t in cfg_targets:
        mountpoints = list(t.mountpoints) if t.mountpoints else ["/"]
        targets.append(
            TargetInput(name=t.name, instance=t.instance, mountpoints=mountpoints, url=t.url)
        )
    return targets
