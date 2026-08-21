"""Юнит-тесты автоответов: расписание, парсинг настроек, матрица выбора.

Чистые функции без БД — вся матрица правил Wazzup-семантики.
"""

from datetime import UTC, datetime

from app.bridge.autoreply import (
    OFFHOURS_DEDUP_SEC,
    AutoReplyConfig,
    in_work_hours,
    parse_work_hours,
    pick_reply,
)

_DEFAULT = (frozenset({0, 1, 2, 3, 4}), 540, 1080)


def test_in_work_hours_boundaries():
    cfg = AutoReplyConfig()  # Пн–Пт 09:00–18:00 Europe/Moscow
    assert in_work_hours(cfg, datetime(2026, 8, 20, 6, 0, tzinfo=UTC))  # 09:00 вкл
    assert not in_work_hours(cfg, datetime(2026, 8, 20, 5, 59, tzinfo=UTC))
    assert in_work_hours(cfg, datetime(2026, 8, 20, 14, 59, tzinfo=UTC))  # 17:59
    assert not in_work_hours(cfg, datetime(2026, 8, 20, 15, 0, tzinfo=UTC))  # 18:00 искл


def test_in_work_hours_weekend():
    cfg = AutoReplyConfig()
    # Суббота 22-08-2026, 10:00 UTC = 13:00 msk — время рабочее, день нет.
    assert not in_work_hours(cfg, datetime(2026, 8, 22, 10, 0, tzinfo=UTC))


def test_in_work_hours_timezone():
    # 05:00 UTC: Москва 08:00 (до смены), Владивосток 15:00 (рабочее).
    ts = datetime(2026, 8, 20, 5, 0, tzinfo=UTC)
    assert not in_work_hours(AutoReplyConfig(), ts)
    assert in_work_hours(AutoReplyConfig(tz="Asia/Vladivostok"), ts)


def test_in_work_hours_naive_is_utc():
    # naive — намеренно: SQLite-тесты хранят naive (коэрс в UTC внутри).
    naive = datetime(2026, 8, 20, 6, 0)  # noqa: DTZ001
    assert in_work_hours(AutoReplyConfig(), naive)


def test_parse_work_hours():
    assert parse_work_hours('{"days":[0,4],"start":"10:00","end":"19:00"}') == (
        frozenset({0, 4}),
        600,
        1140,
    )


def test_parse_work_hours_garbage_falls_back():
    for raw in (None, "не json", '{"days":[],"start":"10:00","end":"19:00"}',
                '{"days":[0],"start":"19:00","end":"10:00"}', '{"days":[0],"start":"xx","end":"10:00"}'):
        assert parse_work_hours(raw) == _DEFAULT


# ---- pick_reply: матрица ----


def test_pick_reply_all_off():
    assert pick_reply(AutoReplyConfig(), is_first_inbound=True, in_hours=True,
                      last_autoreply_age_sec=None, manager_answered=False) is None


def test_pick_reply_first_inbound():
    cfg = AutoReplyConfig(first_enabled=True, first_text="Привет")
    assert pick_reply(cfg, is_first_inbound=True, in_hours=True,
                      last_autoreply_age_sec=None, manager_answered=False) == "Привет"
    assert pick_reply(cfg, is_first_inbound=False, in_hours=True,
                      last_autoreply_age_sec=None, manager_answered=False) is None


def test_pick_reply_offhours():
    cfg = AutoReplyConfig(offhours_enabled=True, offhours_text="Закрыто")
    assert pick_reply(cfg, is_first_inbound=False, in_hours=False,
                      last_autoreply_age_sec=None, manager_answered=False) == "Закрыто"
    # Дедуп: окно ещё не истекло — молчим; истекло — отвечаем.
    assert pick_reply(cfg, is_first_inbound=False, in_hours=False,
                      last_autoreply_age_sec=OFFHOURS_DEDUP_SEC - 60,
                      manager_answered=False) is None
    assert pick_reply(cfg, is_first_inbound=False, in_hours=False,
                      last_autoreply_age_sec=OFFHOURS_DEDUP_SEC + 60,
                      manager_answered=False) == "Закрыто"
    # Живой разговор: менеджер отвечал после предыдущего inbound — не влезаем.
    assert pick_reply(cfg, is_first_inbound=False, in_hours=False,
                      last_autoreply_age_sec=None, manager_answered=True) is None


def test_pick_reply_offhours_wins_over_first_at_night():
    """Ночной первый контакт получает только off-hours текст (без дубля)."""
    cfg = AutoReplyConfig(
        first_enabled=True, first_text="Привет",
        offhours_enabled=True, offhours_text="Закрыто",
    )
    assert pick_reply(cfg, is_first_inbound=True, in_hours=False,
                      last_autoreply_age_sec=None, manager_answered=False) == "Закрыто"


def test_pick_reply_first_fires_at_night_when_offhours_disabled():
    cfg = AutoReplyConfig(first_enabled=True, first_text="Привет")
    assert pick_reply(cfg, is_first_inbound=True, in_hours=False,
                      last_autoreply_age_sec=None, manager_answered=False) == "Привет"


def test_pick_reply_offhours_not_in_work_hours():
    """В рабочее время offhours-триггер не стреляет вовсе."""
    cfg = AutoReplyConfig(
        first_enabled=False, offhours_enabled=True, offhours_text="Закрыто"
    )
    assert pick_reply(cfg, is_first_inbound=False, in_hours=True,
                      last_autoreply_age_sec=None, manager_answered=False) is None
