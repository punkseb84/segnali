from datetime import datetime, timezone

from project.ichimoku_performance_report import (
    calculate_summary,
    format_detail_messages,
    load_performance_rows,
    send_startup_performance_report,
)


class _Postgres:
    def __init__(self, rows, already_sent=False):
        self.rows = rows
        self.already_sent = already_sent
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetch_all(self, sql, params=()):
        if "FROM signals.generated_signals" in sql:
            return self.rows
        if "FROM signals.report_dispatches" in sql:
            return [(1,)] if self.already_sent else []
        return []


class _Notification:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.messages = []

    def send_telegram(self, message):
        self.messages.append(message)
        return {"message_id": len(self.messages)} if self.succeeds else None


def _rows():
    return [
        (
            301,
            datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc),
            "ETH/USD",
            "1h",
            "TARGET_HIT",
            2000.0,
            2000.0,
            2040.0,
            datetime(2026, 7, 28, 13, 0, tzinfo=timezone.utc),
            "ICHIMOKU_CLOUD_EXIT",
            1.20,
            0.0,
            {
                "atr": 10.0,
                "atr_stop_multiple": 1.5,
                "breakeven_armed": True,
                "tp1_hit": True,
                "tp1_price": 2020.0,
            },
        ),
        (
            302,
            datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
            "SOL/USD",
            "1h",
            "STOP_LOSS",
            100.0,
            97.0,
            97.0,
            datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
            "ATR_STOP_ICHIMOKU",
            0.0,
            0.80,
            {"atr": 2.0, "atr_stop_multiple": 1.5},
        ),
        (
            303,
            datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc),
            "BTC/USD",
            "1h",
            "OPEN",
            70000.0,
            69000.0,
            None,
            None,
            "",
            0.0,
            0.0,
            {"atr": 500.0, "atr_stop_multiple": 1.5, "breakeven_armed": False},
        ),
    ]


def test_report_calculates_net_results_budget_and_drawdown():
    postgres = _Postgres(_rows())
    rows = load_performance_rows(postgres, "2026-07-28T04:45:00+00:00")
    summary = calculate_summary(rows, 100.0)

    assert summary.total_signals == 3
    assert summary.closed_signals == 2
    assert summary.open_signals == 1
    assert summary.wins == 1
    assert summary.losses == 1
    assert summary.win_rate == 50.0
    assert round(summary.net_result_eur, 2) == 0.40
    assert round(summary.final_budget_eur, 2) == 100.40
    assert round(summary.max_drawdown_eur, 2) == 0.80
    assert rows[0].initial_stop == 1985.0
    assert rows[0].tp1_price == 2020.0


def test_detail_messages_show_outcome_levels_and_budget():
    rows = load_performance_rows(_Postgres(_rows()), "2026-07-28T04:45:00+00:00")
    calculate_summary(rows, 100.0)
    messages = format_detail_messages(rows)
    joined = "\n".join(messages)

    assert "ETH/USD" in joined
    assert "Uscita cloud" in joined
    assert "+€1,20" in joined
    assert "€101,20" in joined
    assert "1985,00" in joined
    assert "2020,00" in joined


def test_startup_report_is_sent_once_and_marked_only_after_delivery():
    postgres = _Postgres(_rows())
    notification = _Notification()

    sent = send_startup_performance_report(
        postgres,
        notification,
        since_utc="2026-07-28T04:45:00+00:00",
        report_key="test-report",
    )

    assert sent is True
    assert len(notification.messages) == 3
    assert any("INSERT INTO signals.report_dispatches" in sql for sql, _ in postgres.executed)


def test_existing_report_marker_prevents_duplicate_messages():
    postgres = _Postgres(_rows(), already_sent=True)
    notification = _Notification()

    sent = send_startup_performance_report(
        postgres,
        notification,
        report_key="test-report",
    )

    assert sent is False
    assert notification.messages == []


def test_failed_telegram_delivery_does_not_mark_report_sent():
    postgres = _Postgres(_rows())
    notification = _Notification(succeeds=False)

    sent = send_startup_performance_report(
        postgres,
        notification,
        report_key="test-report",
    )

    assert sent is False
    assert not any("INSERT INTO signals.report_dispatches" in sql for sql, _ in postgres.executed)
