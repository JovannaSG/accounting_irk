import pandas as pd


def compare_audits(
    old_details: pd.DataFrame,
    new_details: pd.DataFrame
) -> dict[str, pd.DataFrame]:
    """
    Сравнивает два детальных отчета и возвращает три датафрейма:
    - resolved (Исправлено): было в старом, нет в новом.
    - new (Новые ошибки): не было в старом, появилось в новом.
    - pending (Остались): есть и там, и там.
    """

    if old_details.empty and new_details.empty:
        empty_df = pd.DataFrame(columns=[
            "Проверка", "Уровень",
            "Период", "Счет", "Субконто",
            "Сумма", "Комментарий"
        ])
        return {
            "resolved": empty_df.copy(),
            "new": empty_df.copy(),
            "pending": empty_df.copy()
        }

    def make_key(df: pd.DataFrame) -> pd.Series:
        if df.empty:
            return pd.Series(dtype=str)

        def _get_col(name: str) -> pd.Series:
            if name in df.columns:
                return df[name].fillna("").astype(str).str.strip()
            return pd.Series([""] * len(df), index=df.index)

        # Ключ ошибки: Тип проверки + Счет + Контрагент
        return (
            _get_col("Проверка") + "|"
            + _get_col("Счет") + "|"
            + _get_col("Субконто")
        )

    # Работа только с копиями, без повреждения исходный данных
    if not old_details.empty:
        old_df = old_details.copy()
        old_df["_key"] = make_key(old_df)
        old_keys = set(old_df["_key"])
    else:
        old_df = pd.DataFrame()
        old_df["_key"] = []
        old_keys = set()

    if not new_details.empty:
        new_df = new_details.copy()
        new_df["_key"] = make_key(new_df)
        new_keys = set(new_df["_key"])
    else:
        new_df = pd.DataFrame()
        new_df["_key"] = []
        new_keys = set()

    # Математика множеств
    resolved_keys = old_keys - new_keys
    new_issue_keys = new_keys - old_keys
    pending_keys = old_keys & new_keys

    if not old_df.empty:
        resolved_df = old_df[
            old_df["_key"].isin(resolved_keys)
        ].drop(columns=["_key"])
    else:
        resolved_df = pd.DataFrame(columns=[
            "Проверка", "Уровень", "Период",
            "Счет", "Субконто", "Сумма", "Комментарий"
        ])

    if not new_df.empty:
        new_issues_df = new_df[
            new_df["_key"].isin(new_issue_keys)
        ].drop(columns=["_key"])
    else:
        new_issues_df = pd.DataFrame(columns=[
            "Проверка", "Уровень", "Период",
            "Счет", "Субконто", "Сумма", "Комментарий"
        ])

    if not new_df.empty:
        pending_df = new_df[
            new_df["_key"].isin(pending_keys)
        ].drop(columns=["_key"])
    else:
        pending_df = pd.DataFrame(columns=[
            "Проверка", "Уровень", "Период",
            "Счет", "Субконто", "Сумма", "Комментарий"
        ])

    return {
        "resolved": resolved_df,
        "new": new_issues_df,
        "pending": pending_df
    }
