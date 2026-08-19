"""Модуль отправки отчётов анализа инфраструктуры в мессенджер BotX.

Формирует текстовые отчёты на основе данных AnalysisReport и доставляет их
через BotX API. Поддерживает валидацию конфигурации, хранение истории
уведомлений (в памяти или через Storage) и форматирование статистики
по серверам и контейнерам.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

from app.analyzer.analyzer import (
    AnalysisReport,
    ContainerStat,
    EndpointStatus,
    MetricValue,
    container_hot,
    container_notable,
)
from app.config.config import Config, MessengerConfig
from app.storage.storage import Storage

logger = logging.getLogger("notifier")


@dataclass
class NotificationRecord:
    """Запись об отправленном уведомлении.

    Хранит временную метку, статус доставки, идентификатор чата
    и, при наличии, текст ошибки.
    """

    timestamp: datetime
    success: bool
    chat_id: str
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Преобразовать запись в словарь для сериализации."""
        d = {
            "timestamp": self.timestamp.isoformat(),
            "success": self.success,
            "chat_id": self.chat_id,
        }
        if self.error is not None:
            d["error"] = self.error
        return d


class Client:
    """Клиент для отправки отчётов анализа нагрузки в BotX.

    Управляет HTTP-соединением, форматирует отчёт, отправляет его
    в указанный чат и сохраняет историю уведомлений.
    """

    def __init__(self, store: Optional[Storage] = None):
        """Инициализировать клиент BotX.

        Args:
            store: Опциональное хранилище для персистентного сохранения истории.
                   Если не указано, история хранится в памяти.
        """
        self._http = httpx.AsyncClient(timeout=10.0)
        self._store = store
        self.history: list[NotificationRecord] = []
        self.max_history = 50
        logger.debug("Creating BotX notifier client")

    async def close(self) -> None:
        """Закрыть HTTP-клиент и освободить ресурсы."""
        await self._http.aclose()

    def notifications(self) -> list[NotificationRecord]:
        """Получить список записей об отправленных уведомлениях.

        Returns:
            Список записей уведомлений из хранилища или из памяти.
        """
        if self._store is not None:
            return [
                NotificationRecord(
                    timestamp=datetime.fromisoformat(d["timestamp"]),
                    success=bool(d["success"]),
                    chat_id=str(d["chat_id"]),
                    error=d.get("error"),
                )
                for d in self._store.get_notifications()
            ]
        return list(self.history)

    def _add_record(self, rec: NotificationRecord) -> None:
        if self._store is not None:
            self._store.add_notification(rec.to_dict())
            return
        if len(self.history) >= self.max_history:
            self.history = self.history[1:]
        self.history.append(rec)

    async def send_report(self, cfg: Config, report: AnalysisReport) -> None:
        """Отправить отчёт анализа в чат BotX.

        Форматирует отчёт, отправляет POST-запрос к BotX API и записывает
        результат доставки в историю. При ошибках конфигурации или сети
       抛出 RuntimeError.

        Args:
            cfg: Конфигурация приложения, содержащая параметры BotX.
            report: Отчёт анализа для отправки.

        Raises:
            RuntimeError: При неполной конфигурации BotX, ошибке HTTP-запроса
                          или неуспешном ответе API.
        """
        botx = cfg.notifier.botx
        rec = NotificationRecord(timestamp=datetime.now(), success=False, chat_id=botx.chat_id)

        if not botx.enabled:
            logger.debug("BotX notifier disabled, skipping")
            rec.success = True
            self._add_record(rec)
            return

        if not botx.api_url or not botx.bearer_token or not botx.chat_id:
            err = "botx config is incomplete"
            logger.error(
                "BotX configuration is incomplete has_url=%s has_token=%s has_chat_id=%s",
                bool(botx.api_url),
                bool(botx.bearer_token),
                bool(botx.chat_id),
            )
            rec.error = err
            self._add_record(rec)
            raise RuntimeError(err)

        text = self.format_report(cfg, report)
        logger.debug("Formatted BotX report length=%d", len(text))

        payload = {"chat_id": botx.chat_id, "text": text}
        headers = {"Authorization": f"Bearer {botx.bearer_token}"}

        try:
            resp = await self._http.post(botx.api_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            rec.error = str(exc)
            self._add_record(rec)
            logger.error("HTTP request to BotX failed url=%s error=%s", botx.api_url, exc)
            raise RuntimeError(f"failed to send request to botx: {exc}") from exc

        if not (200 <= resp.status_code < 300):
            rec.error = f"HTTP {resp.status_code}"
            self._add_record(rec)
            logger.error(
                "BotX API returned error status status_code=%d chat_id=%s",
                resp.status_code,
                botx.chat_id,
            )
            raise RuntimeError(f"botx API returned non-2xx status code: {resp.status_code}")

        rec.success = True
        self._add_record(rec)
        logger.info("Report sent to BotX chat_id=%s", botx.chat_id)

    def validate(self, messenger: MessengerConfig) -> Optional[str]:
        """Проверить конфигурацию мессенджера на корректность.

        Проверяет наличие обязательных полей и отсутствие неразрешённых
        плейсхолдеров (${...}) в api_url, bearer_token и chat_id.

        Args:
            messenger: Конфигурация мессенджера для проверки.

        Returns:
            Строка с описанием ошибки или None, если конфигурация корректна.
        """
        if not messenger.enabled:
            return "botx notifier is disabled"
        if not messenger.api_url:
            return "botx api_url is empty"
        if "${" in messenger.api_url:
            return f'botx api_url contains unresolved placeholder "{messenger.api_url}"'
        if not messenger.bearer_token:
            return "botx bearer_token is empty"
        if "${" in messenger.bearer_token:
            return f'botx bearer_token contains unresolved placeholder "{messenger.bearer_token}"'
        if not messenger.chat_id:
            return "botx chat_id is empty"
        if "${" in messenger.chat_id:
            return f'botx chat_id contains unresolved placeholder "{messenger.chat_id}"'
        return None

    def format_report(self, cfg: Config, report: AnalysisReport) -> str:
        """Форматировать отчёт анализа в текстовый блок для BotX.

        Включает статус эндпоинтов, метрики CPU/памяти, дисков и OOM-события
        для каждого целевого сервера, а также статистику по контейнерам.

        Args:
            cfg: Конфигурация приложения (для параметров контейнеров).
            report: Отчёт анализа для форматирования.

        Returns:
            Отформатированный текст отчёта в Markdown-разметке BotX.
        """
        lines: list[str] = ["📊 *Infra Stats Report*"]
        lines.append(f"🕒 *Time:* {report.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        for t in report.targets:
            lines.append(f"🖥 *{t.name}*")

            if t.url:
                parts = " | ".join(f"{s.period}: {_format_endpoint_status(s)}" for s in t.endpoints)
                lines.append(f"   🌐 {t.url}: {parts}")

            if t.cpu:
                lines.append("   📈 CPU: " + _metric_series(t.cpu))
            if t.memory:
                lines.append("   📈 Mem: " + _metric_series(t.memory))

            for d in t.disks:
                mountpoint = "root" if d.mountpoint == "/" else d.mountpoint
                lines.append(f"   💾 {mountpoint}: " + _metric_series(d.metrics))

            for o in t.oom:
                diff_str = ""
                if o.diff is not None:
                    prefix = "+" if o.diff >= 0 else ""
                    diff_str = f" ({prefix}{o.diff})"
                lines.append(f"   💀 OOM ({o.period}): {o.count} kill(s){diff_str}")

            lines.append("")

        containers_part = self.format_containers(cfg, report.containers)
        if containers_part:
            lines.append(containers_part)

        return "\n".join(lines)

    def format_containers(self, cfg: Config, containers: list[ContainerStat]) -> str:
        """Форматировать секцию контейнеров в отчёте.

        Выводит только контейнеры, являющиеся значимыми (превышающие пороги
        изменения или потребления ресурсов). «Горячие» контейнеры помечаются
        предупреждающим индикатором.

        Args:
            cfg: Конфигурация приложения с порогами контейнеров.
            containers: Список статистики по контейнерам.

        Returns:
            Отформатированная текстовая секция или пустая строка,
            если значимых контейнеров нет.
        """
        if not containers:
            return ""

        cc = cfg.containers
        lines = [
            "🐳 *Containers*",
            f"   (изм. >{cc.change_threshold:.0f}% или >{cc.high_threshold:.0f}% ресурсов)",
            "",
        ]

        listed = 0
        for cn in containers:
            if not container_notable(cn, cc):
                continue
            listed += 1

            name = f"*{cn.name}*"
            if container_hot(cn, cc):
                name = "⚠️ " + name
            lines.append(f"   🐳 {name} ({cn.instance})")

            if cn.cpu:
                lines.append("      📈 CPU: " + _metric_series(cn.cpu))
            if cn.memory:
                lines.append("      📈 Mem: " + _metric_series(cn.memory))
            if _first_value(cn.cpu_vm) >= cc.high_threshold:
                lines.append("      📈 CPU/ВМ: " + _metric_series(cn.cpu_vm))
            if _first_value(cn.mem_vm) >= cc.high_threshold:
                lines.append("      📈 Mem/ВМ: " + _metric_series(cn.mem_vm))
            lines.append("")

        if listed == 0:
            return "🐳 *Containers*\n   (значимых изменений нет)\n\n"
        return "\n".join(lines)


def _metric_series(metrics: list[MetricValue]) -> str:
    return " | ".join(_format_metric_with_diff(m) + "%" for m in metrics)


def _format_endpoint_status(s: EndpointStatus) -> str:
    diff = ""
    if s.diff is not None:
        prefix = "+" if s.diff >= 0 else ""
        diff = f" ({prefix}{s.diff:.1f}%)"
    if s.status == "ok":
        return f"✅ OK{diff}"
    if s.status == "down":
        return f"❌ {s.uptime:.1f}%{diff}"
    return "⚪ не мониторится"


def _format_metric_with_diff(m: MetricValue) -> str:
    if m.diff is None:
        return f"{m.period}: {m.value:.1f}"
    prefix = "+" if m.diff >= 0 else ""
    return f"{m.period}: {m.value:.1f} ({prefix}{m.diff:.1f})"


def _first_value(metrics: list[MetricValue]) -> float:
    if not metrics:
        return 0.0
    return metrics[0].value
