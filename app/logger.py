"""Модуль настройки логирования.

Поддерживает два формата вывода:
- ``json`` — структурированные логи для продакшена (ELK/Loki)
- ``text`` — человекочитаемый формат для разработки

Управляется env-переменными:
- ``LOG_LEVEL``: debug / info / warning / error (по умолчанию info)
- ``LOG_FORMAT``: json / text (по умолчанию text)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time


class JsonFormatter(logging.Formatter):
    """Форматтер для структурированных JSON-логов.

    Формат::

        {"time": "2025-01-15T10:30:00", "level": "info", "msg": "..."}

    При наличии исключения добавляется поле ``traceback``.
    """

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["traceback"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging() -> None:
    """Настраивает корневой логгер приложения.

    Читает ``LOG_LEVEL`` и ``LOG_FORMAT`` из env.
    Вывод идёт в stdout (для сбора Docker-логами).

    Env:
        LOG_LEVEL: debug/info/warn/error (default info)
        LOG_FORMAT: json/text (default text)
    """
    level_name = os.environ.get("LOG_LEVEL", "info").lower()
    level = getattr(logging, level_name.upper(), logging.INFO)

    if os.environ.get("LOG_FORMAT", "text").lower() == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
