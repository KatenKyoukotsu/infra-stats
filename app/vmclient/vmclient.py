"""Асинхронный клиент для VictoriaMetrics.

Предоставляет «вежливый» доступ к API VictoriaMetrics с ограничением
конкурентности (semaphore), контролем частоты запросов (token bucket),
автоматическими повторными попытками с экспоненциальным backoff и
поддержкой Retry-After заголовка.

Основные компоненты:
    - VmClient — основной клиент, выполняющий instant-запросы.
    - TokenBucket — алгоритм ограничения частоты запросов.
    - Series — модель данных для одного временного ряда.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("vmclient")

DEFAULT_BASE_BACKOFF = 0.3
DEFAULT_MAX_BACKOFF = 5.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

DEFAULT_MAX_CONCURRENT = 8
DEFAULT_RPS = 20.0


class TokenBucket:
    """Простой token bucket с burst-ёмкостью (как в Go-версии).

    Управляет скоростью доступа к ресурсу, накапливая токены
    с заданной скоростью и максимальным запасом (burst).
    Метод :meth:`wait` блокируется до тех пор, пока не станет
    доступен хотя бы один токен.
    """

    def __init__(self, rate: float, burst: float):
        self._rate = rate
        self._burst = burst
        self._tokens = burst
        self._last = time.monotonic()

    async def wait(self) -> None:
        """Ожидает доступности одного токена и списывает его.

        Если токены отсутствуют, корутина приостанавливается
        на рассчитанное время до пополнения бакета.
        """
        while True:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep((1 - self._tokens) / self._rate)


class Series:
    """Один временной ряд с разобранным значением.

    Содержит словарь меток (label-ы) и числовое значение,
    извлечённое из ответа VictoriaMetrics.
    """

    __slots__ = ("metric", "value")

    def __init__(self, metric: dict[str, str], value: float):
        self.metric = metric
        self.value = value


class VmClient:
    """«Вежливый» клиент к VictoriaMetrics: semaphore + rate-limit + retry.

    Ограничивает одновременные запросы через :class:`asyncio.Semaphore`,
    контролирует частоту через :class:`TokenBucket` и автоматически
    повторяет запросы при временных ошибках (429, 5xx).
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        rps: float = DEFAULT_RPS,
        retries: int = 3,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ):
        """Инициализирует клиент VictoriaMetrics.

        Args:
            base_url: Базовый URL VictoriaMetrics (например, ``http://vm:8428``).
            timeout: Таймаут одного HTTP-запроса в секундах.
            max_concurrent: Максимальное количество одновременных запросов.
            rps: Максимальная частота запросов в секунду (RPS).
            retries: Количество повторных попыток при ошибке.
            base_backoff: Начальная задержка backoff в секундах.
            max_backoff: Максимальная задержка backoff в секундах.
        """
        if max_concurrent <= 0:
            max_concurrent = DEFAULT_MAX_CONCURRENT
        if rps <= 0:
            rps = DEFAULT_RPS
        if retries < 0:
            retries = 0
        if base_backoff <= 0:
            base_backoff = DEFAULT_BASE_BACKOFF
        if max_backoff <= 0:
            max_backoff = DEFAULT_MAX_BACKOFF

        self._base_url = base_url.rstrip("/")
        self._retries = retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._sem = asyncio.Semaphore(max_concurrent)
        self._limiter = TokenBucket(rps, float(max_concurrent))
        self._http = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=max_concurrent,
                max_keepalive_connections=max_concurrent,
            ),
        )

        logger.debug(
            "Creating VictoriaMetrics client url=%s timeout=%s max_concurrent=%d rps=%s retries=%d",
            base_url,
            timeout,
            max_concurrent,
            rps,
            retries,
        )

    async def close(self) -> None:
        """Закрывает underlying HTTP-клиент и освобождает соединения."""
        await self._http.aclose()

    async def ping(self) -> None:
        """Проверяет доступность VictoriaMetrics простым instant-запросом ``up{}``."""
        await self.query("up{}")

    async def query(self, query: str) -> list[Series]:
        """Instant-запрос; semaphore и rate-limiter держатся на время ретраев.

        Выполняет PromQL instant-выражение и возвращает список
        временных рядов :class:`Series`. При неуспешном статусе
        ответа выбрасывает :exc:`RuntimeError`.

        Args:
            query: PromQL-выражение для instant-запроса.

        Returns:
            Список объектов :class:`Series` с метками и числовыми значениями.

        Raises:
            RuntimeError: Если статус ответа ``"error"`` или HTTP-код не 2xx.
        """
        async with self._sem:
            await self._limiter.wait()
            result = await self._get(query)

        if result.get("status") != "success":
            raise RuntimeError(f"vm query not successful: {result.get('status')}")

        series: list[Series] = []
        for item in result.get("data", {}).get("result", []):
            metric = item.get("metric") or {}
            value = _parse_value(item)
            if value is None:
                logger.debug("VM series value parse failed query=%s", query)
                continue
            series.append(Series(metric, value))

        logger.debug("VM query ok query=%s series=%d", query, len(series))
        return series

    async def _get(self, query: str) -> dict:
        """Выполняет HTTP POST-запрос к ``/api/v1/query`` с повторными попытками.

        Реализует экспоненциальный backoff с jitter. Уважает заголовок
        ``Retry-After`` при ответах 503. Прерывает попытки при
        non-retryable ошибках (все кроме 429 и 5xx).

        Args:
            query: PromQL-выражение.

        Returns:
            Тело ответа VictoriaMetrics как dict (JSON).

        Raises:
            RuntimeError: При non-retryable HTTP-ошибке или исчерпании попыток.
            httpx.HTTPError: При сетевой ошибке после исчерпания попыток.
        """
        last_err: Optional[Exception] = None
        url = f"{self._base_url}/api/v1/query"
        delay = self._base_backoff

        for attempt in range(self._retries + 1):
            if attempt > 0:
                await asyncio.sleep(delay * (0.5 + random.random()))

            try:
                resp = await self._http.post(url, data={"query": query})
            except httpx.HTTPError as exc:
                last_err = exc
                logger.warning("VM request failed attempt=%d error=%s", attempt + 1, exc)
                delay = min(delay * 2, self._max_backoff)
                continue

            if resp.status_code in RETRYABLE_STATUSES:
                last_err = RuntimeError(
                    f"vm returned retryable status {resp.status_code}: {resp.text[:200]}"
                )
                logger.warning(
                    "VM returned retryable status attempt=%d status=%d", attempt + 1, resp.status_code
                )
                delay = _next_backoff(delay, resp, self._max_backoff)
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"vm returned status {resp.status_code}: {resp.text[:200]}")

            try:
                return resp.json()
            except ValueError as exc:
                last_err = exc
                logger.warning("VM parse failed attempt=%d error=%s", attempt + 1, exc)
                delay = min(delay * 2, self._max_backoff)
                continue

        raise last_err or RuntimeError("vm request failed")


def _next_backoff(current: float, resp: httpx.Response, max_backoff: float) -> float:
    """Бэкoff после ретраябельного ответа: Retry-After > 503 > общий.

    Приоритет определения задержки: сначала проверяется заголовок
    ``Retry-After`` (если задан и корректен), затем для кода 503
    используется удвоение с минимумом 2 секунды, во всех остальных
    случаях — стандартное удвоение с ограничением ``max_backoff``.

    Args:
        current: Текущая задержка backoff в секундах.
        resp: HTTP-ответ для извлечения ``Retry-After``.
        max_backoff: Максимально допустимая задержка.

    Returns:
        Новая задержка backoff в секундах.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            seconds = float(retry_after)
            if seconds > 0:
                return min(seconds, max_backoff)
        except ValueError:
            pass
    if resp.status_code == 503:
        return max(current * 2, 2.0)
    return min(current * 2, max_backoff)


def _parse_value(item: dict) -> Optional[float]:
    """Извлекает числовое значение из элемента ответа VictoriaMetrics.

    Формат ``value`` в ответе: ``[timestamp_str, value_str]``.
    Метод берёт второй элемент и преобразует его в ``float``.

    Args:
        item: Элемент ``result`` из JSON-ответа VictoriaMetrics.

    Returns:
        Числовое значение или ``None``, если парсинг не удался.
    """
    raw = item.get("value")
    if not raw or len(raw) < 2:
        return None
    try:
        return float(raw[1])
    except (TypeError, ValueError):
        return None
