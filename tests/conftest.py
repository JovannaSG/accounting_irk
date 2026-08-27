import os


def pytest_configure(config):
    """Отключаем аутентификацию в тестах (иначе st.stop() блокирует sidebar).

    load_dotenv(override=False) не перезаписывает уже заданные переменные,
    поэтому достаточно установить AUDIT_USERS="" до запуска тестов.
    """
    os.environ["AUDIT_USERS"] = ""
