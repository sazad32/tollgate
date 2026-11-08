"""Where the stream can be trimmed (ADR 0016): only below what every consumer group has
acknowledged, and only what's older than the replay window."""

from tollgate_billing.consumer import GroupPosition, trim_point

HOUR_MS = 3_600_000
NOW_MS = 1_731_100_000_000
DAY_AGO = NOW_MS - 24 * HOUR_MS


def group(last_delivered: str, oldest_pending: str | None = None) -> GroupPosition:
    return GroupPosition(last_delivered=last_delivered, oldest_pending=oldest_pending)


def test_acknowledged_entries_older_than_the_window_go() -> None:
    # Everything up to 2 days ago was delivered and acknowledged: trim up to the 24 h line.
    point = trim_point([group(f"{NOW_MS - 1000}-0")], NOW_MS, retention_ms=24 * HOUR_MS)
    assert point == f"{DAY_AGO}-0"


def test_entries_not_yet_delivered_stay_however_old() -> None:
    # Billing was down for 3 days: it last read an entry 3 days ago. Nothing after it may go.
    three_days_ago = NOW_MS - 72 * HOUR_MS
    point = trim_point([group(f"{three_days_ago}-5")], NOW_MS, retention_ms=24 * HOUR_MS)
    assert point == f"{three_days_ago}-6"  # MINID keeps this ID and everything after it


def test_an_unacknowledged_entry_and_everything_after_it_stay() -> None:
    old_pending = f"{NOW_MS - 48 * HOUR_MS}-3"
    point = trim_point([group(f"{NOW_MS}-0", old_pending)], NOW_MS, retention_ms=24 * HOUR_MS)
    assert point == old_pending


def test_every_group_is_respected() -> None:
    behind = f"{NOW_MS - 30 * HOUR_MS}-0"  # say, week 13's reconciliation, reading slowly
    groups = [group(f"{NOW_MS}-0"), group(behind)]
    assert trim_point(groups, NOW_MS, retention_ms=24 * HOUR_MS) == f"{NOW_MS - 30 * HOUR_MS}-1"


def test_a_replay_from_the_start_stops_all_trimming() -> None:
    # XGROUP SETID billing 0: the group will read everything again, so nothing may go.
    assert trim_point([group("0-0")], NOW_MS, retention_ms=24 * HOUR_MS) is None


def test_without_any_group_nothing_is_trimmed() -> None:
    assert trim_point([], NOW_MS, retention_ms=24 * HOUR_MS) is None
