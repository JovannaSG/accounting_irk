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

from core import db

AUDIT_USERS_ENV: str = "AUDIT_USERS"

# Параметры PBKDF2 по умолчанию (новые хэши); при проверке используется
# число итераций из самого хэша, поэтому старые записи остаются читаемыми.
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES: int = 16

# Роли
ROLE_ADMIN = "admin"
ROLE_ACCOUNTANT = "accountant"


def _normalize_url(url: object) -> str:
    """
    Нормализация URL базы для сопоставления прав доступа:
    host в нижнем регистре, убираем слэш в конце
    """

    s = str(url or "").strip()
    if not s:
        return ""
    s = s.rstrip("/")
    # Приводим схему и хост к нижнему регистру (путь оставляем как есть)
    if "://" in s:
        scheme, rest = s.split("://", 1)
        host, _, tail = rest.partition("/")
        return f"{scheme.lower()}://{host.lower()}/{tail}"
    return s.lower()


def hash_password(password: str) -> str:
    """
    Хэш пароля в формате «итерации$соль$хэш» (все компоненты в hex)
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


def _verify_stored_hash(stored: str | None, password: str) -> bool:
    """
    Проверяет пароль по сохранённому хэшу формата «итерации$соль$хэш».
    """

    if not stored:
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


def verify(login: str, password: str) -> bool:
    """
    Проверяет пару логин/пароль

    Источник истины — таблица `users` (роли + доступ к базам). Для обратной
    совместимости, если пользователя нет в БД, выполняется фолбэк на
    переменную окружения AUDIT_USERS (логин:хэш).
    """

    login = str(login).strip().lower()

    user = db.get_user(login)
    if user is not None:
        return _verify_stored_hash(user.get("password_hash"), password)

    # Фолбэк на env (старые развёртывания без users.json/БД-пользователей)
    stored = parse_users(os.environ.get(AUDIT_USERS_ENV)).get(login)
    return _verify_stored_hash(stored, password)


def auth_enabled() -> bool:
    """
    Аутентификация включена, когда есть пользователи в БД или AUDIT_USERS.
    """

    try:
        if db.list_users():
            return True
    except Exception:
        pass
    return bool(parse_users(os.environ.get(AUDIT_USERS_ENV)))


def get_user(login: str) -> dict | None:
    """
    Пользователь из БД (или запись, построенная из AUDIT_USERS как fallback)
    """

    login_norm = str(login).strip().lower()
    user = db.get_user(login_norm)
    if user is not None:
        return user
    # Fallback для env-пользователей: роль accountant с пустым списком баз —
    # не имеет доступа ни к одной базе (кроме собственных локальных прогонов)
    env_hash = parse_users(os.environ.get(AUDIT_USERS_ENV)).get(login_norm)
    if env_hash:
        return {
            "login": login_norm,
            "role": ROLE_ACCOUNTANT,
            "password_hash": env_hash,
            "allowed_urls": [],
            "active": True,
        }
    return None


def user_role(login: str) -> str:
    """
    Роль пользователя (admin|accountant); по умолчанию — accountant
    """

    user = get_user(login)
    if not user:
        return ROLE_ACCOUNTANT
    role = str(user.get("role") or ROLE_ACCOUNTANT).strip().lower()
    return role if role in (ROLE_ADMIN, ROLE_ACCOUNTANT) else ROLE_ACCOUNTANT


def user_allowed_urls(login: str) -> list[str]:
    """
    Список URL баз, доступных пользователю (для admin — [] = все)
    """

    user = get_user(login)
    if not user:
        return []
    urls = user.get("allowed_urls") or []
    return [_normalize_url(u) for u in urls if _normalize_url(u)]


def user_can_access(login: str, url: object) -> bool:
    """
    Может ли пользователь работать с базой по URL

    - admin (allowed_urls пуст) — True.
    - accountant — True, только если нормализованный URL в его списке
    """

    user = get_user(login)
    if not user:
        return False
    if user_role(login) == ROLE_ADMIN:
        return True
    norm = _normalize_url(url)
    if not norm:
        return False
    return norm in user_allowed_urls(login)


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
