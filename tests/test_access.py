import json

import pandas as pd
import pytest

import core.auth as auth
import core.db as db_mod


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Изолирует БД (как в test_auth), чтобы не «загрязнять» общую тестовую БД."""
    test_path = str(tmp_path / "access_test.db")
    monkeypatch.setattr(db_mod, "_DB_PATH", test_path)
    yield


def _mk_result(aid, name, user, url=None, stype="batch"):
    meta = {"source_type": stype}
    if url:
        meta["url"] = url
    return {
        "audit_id": aid,
        "db_name": name,
        "accountant": "",
        "viewed_at": "01.01.2026",
        "status": "warning",
        "status_label": "Замечания",
        "total_flags": 1,
        "details": pd.DataFrame([
            {"Проверка": "x", "Уровень": "error", "Счет": "51",
             "Субконто": "-", "Сумма": 1.0, "Комментарий": "c"},
        ]),
        "user": user,
        "meta": meta,
    }


# ── Роли и доступ к базам ──

def test_roles_and_url_access():
    db_mod.upsert_user(
        "ivanova", "accountant", auth.hash_password("pw1"),
        ["https://1cfresh.com/a/ab/123"],
    )
    db_mod.upsert_user("admin", "admin", auth.hash_password("pw2"), [])

    assert auth.user_role("ivanova") == "accountant"
    assert auth.user_role("admin") == "admin"
    # дефолтная роль для неизвестного пользователя — accountant
    assert auth.user_role("nobody") == "accountant"

    # accountant — только свой URL (нормализация слэша/регистра)
    assert auth.user_can_access("ivanova", "HTTPS://1cfresh.com/a/ab/123/")
    assert not auth.user_can_access("ivanova", "https://1cfresh.com/a/cd/456")
    # admin — все базы, даже пустой URL отсутствует в списке
    assert auth.user_can_access("admin", "https://anything/x")
    # неизвестный пользователь не имеет доступа
    assert not auth.user_can_access("nobody", "https://1cfresh.com/a/ab/123")


def test_verify_against_db_users():
    db_mod.upsert_user(
        "petrov", "accountant", auth.hash_password("secret"),
        ["https://1cfresh.com/a/ab/123"],
    )
    assert auth.verify("Petrov", "secret")
    assert not auth.verify("petrov", "wrong")
    assert not auth.verify("sidorov", "secret")


# ── Фильтрация истории по правам ──

def test_history_filtered_by_allowed_urls():
    db_mod.init_db()
    db_mod.save_audit_log(_mk_result("a1", "База А", "ivanova", "https://1cfresh.com/a/ab/123", "batch"))
    db_mod.save_audit_log(_mk_result("b1", "База Б", "petrov", "https://1cfresh.com/a/cd/456", "batch"))
    db_mod.save_audit_log(_mk_result("loc1", "Локальная", "ivanova", None, "file"))

    acc = db_mod.load_audit_history(
        user="ivanova", allowed_urls=["https://1cfresh.com/a/ab/123"]
    )
    assert sorted(e["audit_id"] for e in acc) == ["a1", "loc1"]

    admin = db_mod.load_audit_history(user="admin", allowed_urls=[])
    assert sorted(e["audit_id"] for e in admin) == ["a1", "b1", "loc1"]

    all_h = db_mod.load_audit_history()
    assert len(all_h) == 3


# ── Сид из users.json ──

def test_seed_from_config_file(tmp_path, monkeypatch):
    cfg = {
        "admin": {"role": "admin", "password_hash": auth.hash_password("pw"),
                  "allowed_urls": []},
        "ivanova": {"role": "accountant", "password_hash": auth.hash_password("pw"),
                    "allowed_urls": ["https://1cfresh.com/a/ab/123"]},
    }
    cfg_path = str(tmp_path / "users.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    monkeypatch.setattr(db_mod, "USERS_CONFIG_PATH", cfg_path)
    monkeypatch.delenv(auth.AUDIT_USERS_ENV, raising=False)

    # Первый init_db сеет пользователей из конфига.
    db_mod.init_db()
    assert auth.user_role("admin") == "admin"
    assert auth.user_role("ivanova") == "accountant"
    assert auth.user_can_access("ivanova", "https://1cfresh.com/a/ab/123")
