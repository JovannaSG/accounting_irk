import os
import tempfile

import pytest

_APP_TEST_DIR = tempfile.mkdtemp(prefix="app_test_db_")
_APP_TEST_DB = os.path.join(_APP_TEST_DIR, "app_test.db")


def pytest_configure(config):
    """Отключаем аутентификацию в тестах (иначе st.stop() блокирует sidebar).

    load_dotenv(override=False) не перезаписывает уже заданные переменные,
    поэтому достаточно установить AUDIT_USERS="" до запуска тестов.

    AppTest работает in-process, поэтому core.db._DB_PATH и USERS_CONFIG_PATH
    фиксируются при первом импорте. Чтобы тесты приложения не писали в реальную
    БД проекта, не читали реальный users.json и не «загрязняли» друг друга,
    направляем оба пути в общий временный каталог ДО импорта core.db.
    """
    os.environ["AUDIT_USERS"] = ""
    os.environ["AUDIT_DB_PATH"] = _APP_TEST_DB
    # Настоящий users.json (если создан) не должен сеять пользователей в тестах:
    # указываем на несуществующий путь → load_users_config() вернёт {}.
    os.environ["AUDIT_USERS_CONFIG"] = os.path.join(_APP_TEST_DIR, "users.json")


def pytest_sessionfinish(session, exitstatus):
    """Убираем общий временный файл БД после прогона."""
    db_path = os.environ.get("AUDIT_DB_PATH")
    if db_path and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _clean_app_db():
    """Очищает общую тестовую БД между тестами (чтобы пользователи/аудиты,
    засеянные одним тестом, не влияли на следующий)."""
    yield
    os.environ["AUDIT_USERS"] = ""
    db_path = _APP_TEST_DB
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
