"""Точка входа приложения infra-stats.

Создаёт FastAPI-приложение с lifespan-хуком, который:
1. Загружает конфигурацию
2. Создаёт клиент VM, движок анализа, хранилище, нотификатор
3. Запускает планировщик (cron)
4. Корректно завершает ресурсы при shutdown

Запуск::

    python -m app.main
    # или
    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.analyzer.analyzer import Engine
from app.config.manager import Manager
from app.handlers.handlers import router
from app.logger import setup_logging
from app.notifier.notifier import Client as NotifierClient
from app.scheduler.scheduler import Scheduler
from app.storage.storage import Storage
from app.vmclient.vmclient import VmClient


def create_app() -> FastAPI:
    """Фабрика FastAPI-приложения.

    Lifespan-хук инициализирует все компоненты и привязывает их к ``app.state``:

    - ``app.state.cfg_mgr`` — менеджер конфигурации
    - ``app.state.vm_client`` — клиент VictoriaMetrics
    - ``app.state.engine`` — движок анализа
    - ``app.state.store`` — хранилище отчётов (SQLite)
    - ``app.state.notifier`` — клиент уведомлений
    - ``app.state.scheduler`` — планировщик cron-задач

    StaticFiles монтируется на ``/`` для веб-интерфейса.
    """
    setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config_path = os.environ.get("CONFIG_PATH", "configs/config.yaml")
        cfg_mgr = Manager(config_path)
        cfg = cfg_mgr.get()

        vm_client = VmClient(
            base_url=cfg.victoria_metrics.url,
            timeout=cfg.victoria_metrics.timeout,
            max_concurrent=cfg.victoria_metrics.max_concurrent,
            rps=cfg.victoria_metrics.rps,
            retries=cfg.victoria_metrics.retries,
            base_backoff=cfg.victoria_metrics.base_backoff,
            max_backoff=cfg.victoria_metrics.max_backoff,
        )
        engine = Engine(
            vm_client,
            cfg.analysis,
            cfg.containers,
            cfg.blackbox,
            cache_ttl=cfg.cache_ttl,
            cache_maxsize=cfg.cache_maxsize,
        )
        store = Storage(
            cfg.storage.path,
            max_reports=cfg.storage.max_reports,
            max_notifications=cfg.storage.max_notifications,
        )
        notifier = NotifierClient(store)
        scheduler = Scheduler(cfg_mgr, engine, store, notifier)

        app.state.cfg_mgr = cfg_mgr
        app.state.vm_client = vm_client
        app.state.engine = engine
        app.state.store = store
        app.state.notifier = notifier
        app.state.scheduler = scheduler

        scheduler.start()

        try:
            yield
        finally:
            scheduler.stop()
            await vm_client.close()
            await notifier.close()

    app = FastAPI(title="Infra Stats Analyzer", lifespan=lifespan)
    app.include_router(router)

    static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()


def main() -> None:
    """Запуск uvicorn-сервера.

    Порт берётся из env ``PORT`` (по умолчанию 8080).
    """
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
