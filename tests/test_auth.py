import pytest

import core.auth as auth
import core.db as db_mod


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Изолирует БД пользователей для каждого теста (иначе засев из AUDIT_USERS
    в одном тесте «загрязняет» следующий — auth_enabled становится True)."""
    test_path = str(tmp_path / "auth_test.db")
    monkeypatch.setattr(db_mod, "_DB_PATH", test_path)
    yield


# ── 1. Разбор строки AUDIT_USERS ──

def test_parse_users():
    raw = (
        "Auditor:200000$aa11$bb22, reviewer:11111$cc33$dd44,"
        "битый-фрагмент-без-двоеточия, :пустой_логин, пустой_хэш:"
    )
    users = auth.parse_users(raw)
    assert users == {
        "auditor": "200000$aa11$bb22",
        "reviewer": "11111$cc33$dd44",
    }


def test_parse_users_empty():
    assert auth.parse_users(None) == {}
    assert auth.parse_users("") == {}


# ── 2. Хэширование и проверка пароля ──

def test_hash_password_format():
    stored = auth.hash_password("секрет123")
    parts = stored.split("$")
    # «итерации$соль-hex$хэш-hex»
    assert len(parts) == 3
    assert int(parts[0]) >= 1
    assert len(parts[1]) >= 16
    assert all(c in "0123456789abcdef" for c in parts[1] + parts[2])
    # Соль случайна: два хэша одного пароля не совпадают
    assert stored != auth.hash_password("секрет123")


def test_verify_ok_and_wrong(monkeypatch):
    stored = auth.hash_password("пароль")
    monkeypatch.setenv(auth.AUDIT_USERS_ENV, f"Иванов:{stored}")
    assert auth.verify("иванов", "пароль")  # логин регистронезависимый
    assert not auth.verify("Иванов", "другой")
    assert not auth.verify("петров", "пароль")
    assert not auth.verify("", "пароль")


def test_verify_tolerates_broken_hash(monkeypatch):
    monkeypatch.setenv(auth.AUDIT_USERS_ENV, "a:не-хэш,b:1$zz$xx")
    assert not auth.verify("a", "любой")
    assert not auth.verify("b", "любой")


# ── 3. Включение аутентификации ──

def test_auth_enabled(monkeypatch):
    monkeypatch.delenv(auth.AUDIT_USERS_ENV, raising=False)
    assert not auth.auth_enabled()
    monkeypatch.setenv(auth.AUDIT_USERS_ENV, "a:1$x$y")
    assert auth.auth_enabled()


# ── 4. CLI: python -m core.auth hash <пароль> ──

def test_cli_hash_prints_storable_hash(capsys):
    code = auth.main(["hash", "pw"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out.count("$") == 2
    assert auth.verify("x", "pw") is False  # без env проверка просто False


def test_cli_usage_error(capsys):
    assert auth.main([]) == 2
    assert auth.main(["hash"]) == 2
