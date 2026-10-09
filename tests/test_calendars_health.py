from datetime import UTC, datetime

from conftest import bars_at

from xasset.normalize.calendars import completed_sessions, expected_bar_ends
from xasset.qc.report import health
from xasset.store.writer import merge_bars, writer_lock


def test_nyse_holiday_half_day_and_bar_end_convention():
    # Thanksgiving is closed; Black Friday 2025 is 09:30-13:00 New York.
    holiday = expected_bar_ends(
        "XNYS", datetime(2025, 11, 27, tzinfo=UTC), datetime(2025, 11, 28, tzinfo=UTC)
    )
    assert not holiday
    short = expected_bar_ends(
        "XNYS", datetime(2025, 11, 28, tzinfo=UTC), datetime(2025, 11, 29, tzinfo=UTC)
    )
    assert len(short) == 210
    assert min(short) == datetime(2025, 11, 28, 14, 31, tzinfo=UTC)
    assert max(short) == datetime(2025, 11, 28, 18, 0, tzinfo=UTC)


def test_daylight_saving_and_hong_kong_lunch():
    winter = expected_bar_ends(
        "XNYS", datetime(2026, 3, 6, tzinfo=UTC), datetime(2026, 3, 7, tzinfo=UTC)
    )
    summer = expected_bar_ends(
        "XNYS", datetime(2026, 3, 9, tzinfo=UTC), datetime(2026, 3, 10, tzinfo=UTC)
    )
    assert min(winter).hour == 14
    assert min(summer).hour == 13
    hk = expected_bar_ends(
        "XHKG", datetime(2026, 10, 7, tzinfo=UTC), datetime(2026, 10, 8, tzinfo=UTC)
    )
    assert datetime(2026, 10, 7, 4, 0, tzinfo=UTC) in hk
    assert datetime(2026, 10, 7, 4, 1, tzinfo=UTC) not in hk
    assert datetime(2026, 10, 7, 5, 0, tzinfo=UTC) not in hk
    assert datetime(2026, 10, 7, 5, 1, tzinfo=UTC) in hk


def test_health_proves_five_sessions_and_finds_one_missing_minute(tmp_path, instrument):
    as_of = datetime(2026, 10, 8, tzinfo=UTC)
    sessions = completed_sessions("XNYS", as_of, 5)
    all_times = sorted(time for _, times in sessions for time in times)
    assert len(sessions) == 5
    with writer_lock(tmp_path):
        merge_bars(tmp_path, instrument, bars_at(all_times[:-1]))
    incomplete = health(tmp_path, [instrument], as_of)
    assert not incomplete["ok"]
    assert incomplete["instruments"][0]["sessions"][-1]["missing"] == 1
    with writer_lock(tmp_path):
        merge_bars(tmp_path, instrument, bars_at(all_times[-1:]))
    assert health(tmp_path, [instrument], as_of)["ok"]


def test_health_does_not_require_future_or_open_session_bars(tmp_path, instrument):
    as_of = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    sessions = completed_sessions("XNYS", as_of, 1)
    assert sessions[0][0] == "2026-10-06"
    with writer_lock(tmp_path):
        merge_bars(tmp_path, instrument, bars_at(sorted(sessions[0][1])))
    assert health(tmp_path, [instrument], as_of, sessions=1)["ok"]


def test_missing_data_and_unknown_calendar_cannot_pass(tmp_path, instrument):
    as_of = datetime(2026, 10, 8, tzinfo=UTC)
    assert not health(tmp_path, [instrument], as_of)["ok"]
    proxy = instrument.model_copy(update={"calendar": None, "session": "all"})
    report = health(tmp_path, [proxy], as_of)
    assert not report["ok"]
    assert report["instruments"][0]["status"] == "unknown"
