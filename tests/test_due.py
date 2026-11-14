"""What the evening reminder says, from a saved log. Mirrors the rules the app uses on Today."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import gecko_sync as gs

KL = ZoneInfo("Asia/Kuala_Lumpur")
TODAY = date(2026, 11, 14)


def at(days_ago: int, hour: int = 20) -> str:
    local = datetime(TODAY.year, TODAY.month, TODAY.day, hour, 0, tzinfo=KL) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def gecko(**kw):
    return {"id": "n1", "name": "Nugget", "hatch": "2024-08-17", **kw}


def feed(days_ago, dust="cal"):
    return {"g": "n1", "t": "feed", "at": at(days_ago), "d": {"dust": dust}}


def weight(days_ago, grams=56.8):
    return {"g": "n1", "t": "weight", "at": at(days_ago), "d": {"g": grams}}


def test_stage_follows_the_hatch_date():
    assert gs.stage_of({"hatch": "2026-10-20"}, TODAY) == "hatchling"
    assert gs.stage_of({"hatch": "2026-06-01"}, TODAY) == "juvenile"
    assert gs.stage_of({"hatch": "2026-01-01"}, TODAY) == "subadult"
    assert gs.stage_of({"hatch": "2024-08-17"}, TODAY) == "adult"
    assert gs.stage_of({"stage": "juvenile"}, TODAY) == "juvenile"


def test_adult_fed_yesterday_needs_nothing():
    log = {"geckos": [gecko()], "logs": [feed(1, "d3"), feed(4, "multi"), weight(2)]}
    assert gs.due_today(log, KL, TODAY) == []


def test_meal_due_after_three_days_with_the_right_dusting():
    log = {"geckos": [gecko()], "logs": [feed(3, "cal"), feed(6, "d3"), feed(9, "multi"), weight(2)]}
    assert gs.due_today(log, KL, TODAY) == ["Nugget’s meal is due."]
    log["logs"] = [feed(3, "cal"), feed(20, "d3"), feed(9, "multi"), weight(2)]
    assert gs.due_today(log, KL, TODAY) == ["Nugget’s meal is due. Dust with calcium + D3."]


def test_weigh_in_reminder_after_two_weeks_for_an_adult():
    log = {"geckos": [gecko()], "logs": [feed(1, "d3"), feed(4, "multi"), weight(15)]}
    assert gs.due_today(log, KL, TODAY) == ["Time to weigh Nugget."]


def test_a_custom_schedule_wins_over_the_stage_default():
    log = {"geckos": [gecko(feedEvery=7)], "logs": [feed(5, "d3"), feed(8, "multi")]}
    assert gs.due_today(log, KL, TODAY) == []


def test_first_meal_ever_is_due():
    assert gs.due_today({"geckos": [gecko()], "logs": []}, KL, TODAY)[0].startswith("Nugget’s meal is due")


def test_days_count_in_the_owner_timezone_not_utc():
    # 01:30 in Kuala Lumpur is still the previous day in UTC; it must count as today.
    late = datetime(2026, 11, 14, 1, 30, tzinfo=KL).astimezone(timezone.utc).isoformat()
    assert gs.local_day(late, KL) == TODAY
