"""
Загрузка настроек из файла .env в корне проекта.

Файл опционален: если его нет, используются только реальные переменные
окружения. Уже заданные переменные окружения имеют приоритет над .env —
удобно для Docker/системных сервисов, где значения передаются напрямую.
"""

from __future__ import annotations

import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_project_env(path: str | None = None) -> str | None:
    """
    Читает .env в переменные окружения (существующие не перезаписывает).

    Возвращает путь к файлу, если он найден и загружен, иначе None.
    """

    from dotenv import load_dotenv

    env_path = path or os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return None
    load_dotenv(env_path, override=False)
    return env_path
