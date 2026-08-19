"""Потокобезопасный менеджер конфигурации.

Предоставляет доступ к текущему ``Config`` и позволяет
перезаписать его (при горячем обновлении через API).
После ``save()`` необходимо вызвать ``apply_env_overrides()``,
чтобы секреты из env не были потеряны.

Пример::

    mgr = Manager("configs/config.yaml")
    cfg = mgr.get()
    # ... 修改 cfg ...
    mgr.save(cfg)
    mgr.apply_env_overrides()  # вернуть env-секреты
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict

import yaml

from app.config.config import Config, load_config

# Маппинг env-переменных на пути в Config (для apply_env_overrides).
_ENV_OVERRIDES = {
    "BOTX_BEARER_TOKEN": ("notifier", "botx", "bearer_token"),
    "BOTX_CHAT_ID": ("notifier", "botx", "chat_id"),
    "STORAGE_PATH": ("storage", "path"),
    "API_KEY": ("api_key",),
}


class Manager:
    """Потокобезопасный держатель текущей конфигурации.

    Использует ``threading.Lock`` для безопасного чтения/записи
    из разных async-задач и sync-потоков (scheduler).

    Attributes:
        _path: Путь к YAML-файлу конфигурации.
        _cfg: Текущая конфигурация.
        _lock: Mutex для потокобезопасного доступа.
    """

    def __init__(self, path: str):
        self._path = path
        self._cfg = load_config(path)
        self._lock = threading.Lock()

    def get(self) -> Config:
        """Возвращает текущую конфигурацию (копию ссылки).

        Потокобезопасно. Возвращённый объект может быть изменён
        без влияния на внутреннее состояние (new Config → save).
        """
        with self._lock:
            return self._cfg

    def save(self, new_cfg: Config) -> None:
        """Записывает новую конфигурацию в YAML-файл и обновляет内存.

        После вызова ``apply_env_overrides()`` для восстановления секретов.
        """
        data = yaml.safe_dump(
            asdict(new_cfg), default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        with self._lock:
            with open(self._path, "w", encoding="utf-8") as f:
                f.write(data)
            self._cfg = new_cfg

    def apply_env_overrides(self) -> None:
        """Перечитывает env-переменные и налагает их на текущий конфиг.

        Вызывать после ``save()``, чтобы секреты из env не были потеряны.
        Не перечитывает YAML — только обновляет in-memory конфиг.
        """
        with self._lock:
            cfg = self._cfg
            if env := os.environ.get("BOTX_BEARER_TOKEN"):
                cfg.notifier.botx.bearer_token = env
            if env := os.environ.get("BOTX_CHAT_ID"):
                cfg.notifier.botx.chat_id = env
            if env := os.environ.get("STORAGE_PATH"):
                cfg.storage.path = env
            if env := os.environ.get("API_KEY"):
                cfg.api_key = env
