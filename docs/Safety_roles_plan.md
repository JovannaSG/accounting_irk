# План: Безопасность + роли и ограничение доступа бухгалтеров к базам

Дата: 2026-09-01
Статус: план (к реализации)

## Контекст / задача

1. Провести ревизию кода на безопасность (credential handling, хранение секретов, доступ).
2. Ввести роли: `admin` и `accountant`.
3. Обеспечить, чтобы каждый бухгалтер имел доступ **только к назначенным ему базам**.

---

## A. Найденные проблемы безопасности

### A1. КРИТИЧНО — пароли базами в открытом виде в git
- `client_databases.json` содержит `login`/`password` для всех 116 баз.
- Файл **закоммичен в git** (зафиксирован в `67e761d`, позже добавлен в `.gitignore` —
  но из-за этого он **остаётся в индексе и истории**). Пароли восстанавливаются из `git log`.
- `Dockerfile` выполняет `COPY client_databases.json` — секреты попадают в образ.

### A2. КРИТИЧНО — нет изоляции баз по пользователю
- Режим «🚀 Аудит всех баз» (`fetch_batch`, app/ui.py:768-839) перебирает **все** `_batch_entries`
  для любого залогиненного пользователя.
- Дашборд `_render_dashboard(history)` и вкладка сравнения показывают **все** записи аудита
  любому пользователю (app/ui.py:1201-1209).

### A3. ВЫСОКО — история не фильтруется по пользователю
- `load_audit_history()` (core/db.py:235) возвращает все строки без учёта текущего пользователя.
- Сравнение баз и сводный дашборд раскрывают данные других бухгалтеров.

### A4. ВЫСОКО — слабая работа с паролями в окружении/UI
- app/ui.py:594 — поле пароля 1С предзаполняется из `ONEC_PASS` (видно в DOM).
- `login_data.txt` лежит на диске с учётными данными (gitignored, но всё же секрет).

### A5. СРЕДНЕ — плоский формат `AUDIT_USERS` (login:hash)
- Не несёт ролей и не задаёт доступ к базам (core/auth.py:47).

---

## B. Целевая модель: роли и доступ к базам

Хранение пользователей — в SQLite (рядом с `audits`), управление — конфиг-файлом (без админ-UI),
сопоставление баз — **по URL** (нормализованному).

### B1. Таблица `users` (в `audit_history.db`)
| поле | тип | описание |
|---|---|---|
| `login` | TEXT PK | логин (lowercase) |
| `role` | TEXT | `admin` \| `accountant` |
| `password_hash` | TEXT | PBKDF2-HMAC-SHA256 (формат core.auth) |
| `allowed_urls` | TEXT | JSON-массив URL баз |
| `created_at` | TEXT | дата создания |
| `active` | INTEGER | 1/0 |

Семантика `allowed_urls`:
- `admin` → пустой список = «все базы».
- `accountant` → только перечисленные URL.

### B2. Управление пользователями — конфиг-файл
Новый файл `users.json` в корне (gitignored; шаблон `users.example.json` — в репо).
Формат:

```json
{
  "admin":   {"role": "admin",      "password_hash": "200000$...", "allowed_urls": []},
  "ivanova": {"role": "accountant", "password_hash": "200000$...", "allowed_urls": [
      "https://1cfresh.com/a/ab/123"
  ]}
}
```

- Хэш генерируется CLI: `python -m core.auth hash <пароль>`.
- При старте `init_db()` апсортит пользователей из `users.json` (без деструктивного
  перезаписывания уже созданных в БД).
- Если таблица пуста — как запасной вариант сидер: первый `admin` из `AUDIT_USERS`.

### B3. Правила доступа
- `user_can_access(user, url)`:
  - `admin` → True (управление охватом через admin-набор).
  - `accountant` → True, если нормализованный URL/имя базы в `allowed_urls`.
  - Файловый режим (`📁`) и «тестовые данные» — локальный прототип, без подключения к базе:
    такие аудиты видимы **только своему владельцу** (фильтр `user == login`).
- Нормализация URL: `rstrip('/')`, `lower()` host — чтобы обойти обход по пути/слэшу/регистру.

---

## C. Изменения по файлам

### core/auth.py
- Перевести источник истины на таблицу `users` (с фолбэком на env `AUDIT_USERS` как сид).
- Оставить сигнатуру `verify(login, password)`.
- Добавить хелперы: `get_user`, `list_users`, `save_user`, `upsert_users(config)`,
  `user_allowed_urls`, `user_can_access`, `auth_role`.
- Сохранить `hash_password` + CLI `python -m core.auth hash <пароль>`.

### core/db.py
- Миграция: CREATE TABLE IF NOT EXISTS `users`.
- CRUD пользователей + чтение конфиг-файла `users.json`.
- `load_audit_history(user=None)`: при заданном пользователе фильтровать записи
  (по URL базы из `allowed_urls`; для файловых/локальных — по `user == login`).
- `save_audit_log`: при сохранении записывать `source_type`/`url` из меты, чтобы можно
  было фильтровать по URL.

### app/ui.py
- При входе сохранять в `session_state` `user_role`, `user_allowed_urls`.
- Режим «🚀»: фильтровать `_batch_entries` по `user_can_access` (админы — все).
  Если у бухгалтера нет доступных баз — информационное сообщение и останов.
- Режим «☁️» (одиночный OData): блокировать подключение бухгалтера к URL вне его списка.
- Дашборд/сравнение: фильтровать `history` по правам текущего пользователя перед рендером.
- Убрать предзаполнение `api_pass` из `ONEC_PASS` (app/ui.py:594) — оставить поле пустым.

### Dockerfile / .dockerignore / git
- Убрать `COPY client_databases.json` из Dockerfile (секреты не должны быть в образе);
  в compose уже есть read-only volume mount — этого достаточно.
- `git rm --cached client_databases.json` (оставить в `.gitignore`).
- `users.json` и `client_databases.json` — в `.dockerignore`/`.gitignore`.
- Ротация паролей: т.к. пароли были в git-истории, рекомендуется сменить пароли баз.

### Документация/шаблоны
- Новый `users.example.json` в корне репо.
- Обновить README и, при необходимости, `docs/ДЛЯ_ЗАКАЗЧИКА.md`.

### Тесты
- `tests/test_auth.py` — адаптировать под DB-хранение + фолбэк на env.
- Новый `tests/test_access.py`:
  - `user_can_access` для admin (все) и accountant (только свой URL);
  - нормализация URL (слэш/регистр/путь);
  - фильтрация истории по пользователю;
  - сидер из `users.json` и первый admin из `AUDIT_USERS`.

---

## D. Порядок реализации (check-list)

1. [x] core/db.py: таблица `users` + CRUD + чтение `users.json` + сид-логика.
2. [x] core/auth.py: переключение на DB, новые хелперы, сохранение совместимости `verify`.
3. [x] `users.example.json` (шаблон) + `.gitignore`/`.dockerignore`.
4. [x] app/ui.py: вход → role/grants в session; фильтры batch/одиночного OData; фильтр дашборда/истории.
5. [x] app/ui.py: убрать предзаполнение `api_pass` из `ONEC_PASS`.
6. [x] Dockerfile: убрать `COPY client_databases.json`.
7. [x] `git rm --cached client_databases.json` (без удаления с диска).
8. [x] `load_audit_history(user=...)` через URL/owner-фильтр; `save_audit_log` пишет url/source_type.
9. [x] Тесты: `test_auth.py` + новый `test_access.py`.
10. [x] README/docs: описать роли, конфиг `users.json`, процедуру добавления бухгалтера.
11. [x] Прогнать полный `pytest` + `ruff`.

---

## E. Открытые вопросы / решения по умолчанию

- Первый admin: если таблица пуста — сид из `AUDIT_USERS`; приоритет — явный `admin` в `users.json`.
- Файловые/локальные аудиты: видны только владельцу (`user == login`).
- Нормализация URL: host в lowercase, `/` в конце убраны.
- Управление — конфиг-файлом (без админ-UI), как выбрано.
