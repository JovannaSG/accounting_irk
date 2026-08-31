import os

from core.appenv import load_project_env


# ── 1. Переменные из .env попадают в окружение ──

def test_loads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("APPENV_TEST_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('APPENV_TEST_VAR=привет\n', encoding="utf-8")

    loaded = load_project_env(str(env_file))

    assert loaded == str(env_file)
    assert os.environ["APPENV_TEST_VAR"] == "привет"


# ── 2. Уже заданные переменные окружения имеют приоритет ──

def test_existing_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("APPENV_TEST_VAR", "из-окружения")
    env_file = tmp_path / ".env"
    env_file.write_text('APPENV_TEST_VAR=из-файла\n', encoding="utf-8")

    load_project_env(str(env_file))

    assert os.environ["APPENV_TEST_VAR"] == "из-окружения"


# ── 3. Нет файла — ничего не ломается ──

def test_missing_file_returns_none(tmp_path):
    assert load_project_env(str(tmp_path / "нет_такого.env")) is None
