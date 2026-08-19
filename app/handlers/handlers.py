"""REST API обработчики приложения infra-stats.

Модуль содержит обработчики эндпоинтов для управления мониторингом инфраструктуры:
healthcheck, получение отчётов, ручной запуск анализа, управление конфигурацией,
тестирование подключений и отправка уведомлений в Clouds.

Все эндпоинты используют общее состояние приложения (app.state) для доступа
к хранилищу отчётов, менеджеру конфигурации, планировщику и нотификатору.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.analyzer.analyzer import trim_report_containers

logger = logging.getLogger("handlers")
router = APIRouter()

_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _state(request: Request):
    return request.app.state


def _require_api_key(request: Request) -> None:
    """Проверяет API-ключ для write-эндпоинтов. Ключ задаётся через env API_KEY."""
    api_key = _state(request).cfg_mgr.get().api_key
    if not api_key:
        return
    auth_header = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if auth_header != api_key:
        raise HTTPException(status_code=403, detail="invalid or missing API key")


@router.get("/healthcheck")
async def healthcheck():
    """Проверка доступности сервиса.

    Простой healthcheck-эндпоинт для мониторинга и балансировщиков нагрузки.
    Возвращает plain-text ответ "ItsOK" с HTTP 200 при успешной работе сервиса.
    """
    return PlainTextResponse("ItsOK")


@router.get("/api/status")
async def api_status(request: Request):
    """Получение текущего статуса системы мониторинга.

    Возвращает последний отчёт о состоянии контейнеров с фильтрацией
    по конфигурации (только активные контейнеры).

    Returns:
        dict: Словарь с данными отчёта (метрики, контейнеры, метки времени).

    Raises:
        HTTPException 404: Если отчёты ещё не были сгенерированы.
    """
    report, ok = _state(request).store.get_last_report()
    if not ok:
        raise HTTPException(status_code=404, detail="no reports yet")
    cc = _state(request).cfg_mgr.get().containers
    return trim_report_containers(report, cc).to_dict()


@router.get("/api/reports")
async def api_reports(request: Request):
    """Получение списка всех сохранённых отчётов.

    Возвращает полный список отчётов, хранящихся в хранилище,
    включая исторические данные за все периоды мониторинга.

    Returns:
        list[dict]: Список словарей с данными отчётов, отсортированных
        по времени создания (от новых к старым).
    """
    return [r.to_dict() for r in _state(request).store.get_all_reports()]


@router.post("/api/analyze")
async def api_analyze(request: Request):
    """Запуск ручного анализа нагрузки системы.

    Принудительно запускает немедленный анализ текущего состояния
    инфраструктуры, минуя расписание планировщика. Требует валидный API-ключ.

    Returns:
        dict: Данные нового отчёта с результатами анализа.

    Raises:
        HTTPException 403: При отсутствии или невалидном API-ключе.
    """
    _require_api_key(request)
    logger.info("Manual analysis triggered via API")
    report = await _state(request).scheduler.analyze_now()
    return report.to_dict()


@router.get("/api/containers")
async def api_containers(request: Request):
    """Получение списка контейнеров из последнего отчёта.

    Возвращает подробную информацию о каждом контейнере:
    идентификатор, имя, потребление ресурсов, состояние.

    Returns:
        list[dict]: Список словарей с данными контейнеров.

    Raises:
        HTTPException 404: Если отчёты ещё не были сгенерированы.
    """
    report, ok = _state(request).store.get_last_report()
    if not ok:
        raise HTTPException(status_code=404, detail="no reports yet")
    return [c.to_dict() for c in report.containers]


@router.post("/api/clear")
async def api_clear(request: Request):
    """Очистка хранилища отчётов.

    Удаляет все сохранённые отчёты из хранилища. Требует валидный API-ключ.
    Операция необратима.

    Returns:
        dict: Подтверждение очистки {"status": "cleared"}.

    Raises:
        HTTPException 403: При отсутствии или невалидном API-ключе.
    """
    _require_api_key(request)
    _state(request).store.clear()
    return {"status": "cleared"}


@router.get("/api/config")
async def api_config(request: Request):
    """Получение текущей конфигурации приложения.

    Возвращает полную конфигурацию в виде словаря, включая настройки
    планировщика, нотификатора, список контейнеров и API-ключ.

    Returns:
        dict: Словарь с текущей конфигурацией приложения.
    """
    return _state(request).cfg_mgr.get().to_dict()


@router.get("/api/scheduler")
async def api_scheduler(request: Request):
    """Получение информации о планировщике задач.

    Возвращает cron-расписание анализа и отправки, а также текущий статус
    планировщика (активен, время последнего запуска, следующий запуск).

    Returns:
        dict: Словарь с полями analyze_cron, send_cron и status.
    """
    cfg = _state(request).cfg_mgr.get()
    return {
        "analyze_cron": cfg.scheduler.analyze_cron,
        "send_cron": cfg.scheduler.send_cron,
        "status": _state(request).scheduler.status_dict(),
    }


@router.get("/api/notifications")
async def api_notifications(request: Request):
    """Получение истории отправленных уведомлений.

    Возвращает список всех уведомлений, отправленных в Clouds,
    включая статус отправки и временные метки.

    Returns:
        list[dict]: Список словарей с данными уведомлений.
    """
    return [n.to_dict() for n in _state(request).notifier.notifications()]


@router.post("/api/test/vm")
async def api_test_vm(request: Request):
    """Тестирование подключения к виртуальной машине.

    Выполняет ping-проверку доступности ВМ с таймаутом 5 секунд.
    Используется для верификации сетевой связности перед запуском анализа.

    Returns:
        dict: Результат теста. {"success": true} при успехе,
        {"success": false, "error": "..."} при ошибке подключения.
    """
    try:
        async with asyncio.timeout(5):
            await _state(request).vm_client.ping()
    except Exception as exc:
        logger.warning("VM connectivity test failed error=%s", exc)
        return {"success": False, "error": f"vm ping failed: {exc}"}
    return {"success": True}


@router.post("/api/test/clouds")
async def api_test_clouds(request: Request):
    """Тестирование подключения к Clouds API.

    Проверяет валидность конфигурации бота Clouds: доступность API
    и корректность chat_id. Не отправляет реальные сообщения.

    Returns:
        dict: Результат проверки. {"success": true, "api_url": "...", "chat_id": "..."}
        при успехе, {"success": false, "error": "..."} при ошибке конфигурации.
    """
    cfg = _state(request).cfg_mgr.get()
    err = _state(request).notifier.validate(cfg.notifier.botx)
    if err:
        return {"success": False, "error": err}
    return {"success": True, "api_url": cfg.notifier.botx.api_url, "chat_id": cfg.notifier.botx.chat_id}


@router.post("/api/test/send")
async def api_test_send(request: Request):
    """Тестовая отправка отчёта в Clouds.

    Валидирует конфигурацию Clouds, берёт последний отчёт из хранилища
    и отправляет его в указанный чат. Используется для проверки
    полной цепочки отправки уведомлений.

    Returns:
        dict: {"success": true} при успешной отправке,
        {"success": false, "error": "..."} при ошибке.

    Raises:
        Логирует ошибки, но не выбрасывает исключения наружу.
    """
    st = _state(request)
    cfg = st.cfg_mgr.get()
    err = st.notifier.validate(cfg.notifier.botx)
    if err:
        return {"success": False, "error": err}
    report, ok = st.store.get_last_report()
    if not ok:
        return {"success": False, "error": "no reports available"}
    try:
        await st.notifier.send_report(cfg, report)
    except Exception as exc:
        logger.error("Test send failed error=%s", exc)
        return {"success": False, "error": str(exc)}
    return {"success": True}


@router.get("/api/preview")
async def api_preview(request: Request):
    """Предпросмотр текста отчёта перед отправкой.

    Форматирует последний отчёт в текстовое представление, которое будет
    отправлено в Clouds. Позволяет проверить содержимое уведомления
    до фактической отправки.

    Returns:
        dict: {"text": "...", "timestamp": "..."} с отформатированным
        текстом отчёта и его временной меткой, или {"error": "no reports available"}.
    """
    report, ok = _state(request).store.get_last_report()
    if not ok:
        return {"error": "no reports available"}
    text = _state(request).notifier.format_report(_state(request).cfg_mgr.get(), report)
    return {"text": text, "timestamp": report.timestamp.isoformat()}
