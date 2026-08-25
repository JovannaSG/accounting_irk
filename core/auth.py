"""
Ограничение доступа к приложению и журналирование пользователя запуска
(ТЗ §11).

Учетные записи задаются переменной окружения AUDIT_USERS в формате
«логин:хэш,логин2:хэш2». Хэш генерируется консольной утилитой:

    python -m core.auth hash пароль

Если AUDIT_USERS не задан или пуст, аутентификация отключена — приложение
работает без входа (ручной режим загрузки файлов остается как есть).
Пароли в открытом виде не хранятся: только PBKDF2-HMAC-SHA256 в формате
«итераций$соль$хэш».
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys

AUDIT_USERS_ENV: str = "AUDIT_USERS"

# Параметры PBKDF2 по умолчанию (новые хэши); при проверке используется
# число итераций из самого хэша, поэтому старые записи остаются читаемыми.
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES: int = 16


def hash_password(password: str) -> str:
    """
    Хэш пароля в формате «итерации$соль$хэш» (все компоненты в hex).
    """

    salt_hex = secrets.token_hex(_SALT_BYTES)
    digest_hex = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        _PBKDF2_ITERATIONS,
    ).hex()
    return f"{_PBKDF2_ITERATIONS}${salt_hex}${digest_hex}"


def parse_users(raw: str | None) -> dict[str, str]:
    """
    Разбирает строку AUDIT_USERS в словарь {логин: хэш}
    """

    users: dict[str, str] = {}
    if not raw:
        return users

    # Используем for вместо while для большей читаемости и скорости
    for fragment in raw.split(","):
        login, sep, stored = fragment.partition(":")
        if sep and login.strip() and stored.strip():
            users[login.strip().lower()] = stored.strip()
    return users


def verify(login: str, password: str) -> bool:
    """
    Проверяет пару логин/пароль по AUDIT_USERS
    """

    stored = parse_users(os.environ.get(AUDIT_USERS_ENV)).get(
        str(login).strip().lower()
    )
    if not stored:
        # ИСПРАВЛЕНО: используем _PBKDF2_ITERATIONS, чтобы время
        # вычисления в точности совпадало с реальным логином.
        hashlib.pbkdf2_hmac("sha256", b"login", b"salt", _PBKDF2_ITERATIONS)
        return False

    try:
        iterations_text, salt_text, digest_text = stored.split("$", 2)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_text),
            max(1, int(iterations_text)),
            dklen=len(bytes.fromhex(digest_text)),
        )
    except ValueError:
        return False

    return hmac.compare_digest(actual, bytes.fromhex(digest_text))


def auth_enabled() -> bool:
    """
    Аутентификация включена только когда AUDIT_USERS непустой.
    """

    return bool(parse_users(os.environ.get(AUDIT_USERS_ENV)))


def main(argv: list[str]) -> int:
    """
    Консольная утилита: python -m core.auth hash <пароль>
    """

    if len(argv) == 2 and argv[0] == "hash":
        print(hash_password(argv[1]))
        return 0
    print("Использование: python -m core.auth hash <пароль>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
