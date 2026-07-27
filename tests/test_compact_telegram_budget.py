from project.audited_formatters import format_audited_outcome_message
from project.notification_engine.simple_formatters import format_simple_signal_message
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus


def test_ichimoku_signal_is_compact_and_keeps_core_levels():
    message = format_simple_signal_message(
        {
            "id": 245,
            "strategy": "Ichimoku Cloud Breakout",
            "pair": "ETH/USD",
            "timeframe": "1h",
            "entry": 1893.47,
            "stop_loss": 1884.77,
            "entry_notional_eur": 100.0,
            "score_breakdown": {"atr": 5.8},
        }
    )

    assert "NUOVO SEGNALE" in message
    assert "Entrata" in message
    assert "Stop Loss" in message
    assert "TP1 50%" in message
    assert "Score" not in message
    assert "Perché" not in message


def test_final_outcome_contains_rolling_budget():
    message = format_audited_outcome_message(
        {
            "signal_id": 245,
            "pair": "ETH/USD",
            "timeframe": "1h",
            "realized_net_eur": 1.21,
            "budget_before_eur": 100.0,
            "budget_after_eur": 101.21,
            "tp1_hit": True,
            "tp1_realized_net_eur": 0.79,
        }
    )

    assert "OPERAZIONE CHIUSA" in message
    assert "Budget iniziale: <b>€100.00</b>" in message
    assert "Budget aggiornato: <b>€101.21</b>" in message
    assert "Candela" not in message
    assert "Exchange" not in message


class _Postgres:
    def fetch_all(self, sql, params=()):
        return [(3.50,)]


def test_budget_enrichment_uses_cumulative_closed_results(monkeypatch):
    monkeypatch.setenv("PAPER_INITIAL_BUDGET_EUR", "100")
    engine = ReliableNotificationEngine(EventBus(), enabled=False, postgres=_Postgres())
    payload = {"realized_net_eur": 1.25}

    engine._attach_budget_report(payload)

    assert payload["budget_before_eur"] == 102.25
    assert payload["budget_after_eur"] == 103.50


def test_partial_tp_does_not_update_budget():
    engine = ReliableNotificationEngine(EventBus(), enabled=False, postgres=_Postgres())
    payload = {"partial_take_profit": True, "realized_net_eur": 0.50}

    engine._attach_budget_report(payload)

    assert "budget_before_eur" not in payload
