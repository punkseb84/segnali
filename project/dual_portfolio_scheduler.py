"""One collector, two isolated PAPER engines: public signals and private spot portfolio."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from project.personal_intraday_monitor import PersonalIntradayMonitor
from project.personal_intraday_scanner import PersonalIntradayScanner
from project.relative_strength_scheduler import RelativeStrengthScheduler

ROME = ZoneInfo("Europe/Rome")


class DualPortfolioScheduler(RelativeStrengthScheduler):
    """Keep the public scanner active and run the private selector only at 06:00 Rome."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        public_scanner = self.strategy_engine
        public_monitor = self.position_monitor
        if public_scanner is None or public_monitor is None:
            raise RuntimeError("Dual runtime requires public scanner and monitor")

        self.personal_scanner = PersonalIntradayScanner(
            public_scanner.client,
            public_scanner.event_bus,
            public_scanner.pairs,
            exchange=public_scanner.exchange,
            trade_notional_eur=10.0,
            buy_fee_rate=public_scanner.buy_fee_rate,
            sell_fee_rate=public_scanner.sell_fee_rate,
            spread_rate=public_scanner.spread_rate,
            slippage_rate=public_scanner.slippage_rate,
            quantity_step=public_scanner.quantity_step,
            min_qty=public_scanner.min_qty,
            min_notional_eur=min(10.0, public_scanner.min_notional_eur),
            signal_cooldown_minutes=24 * 60,
            min_signal_score=0.0,
            min_net_rr=0.0,
            min_net_profit_eur=0.0,
            enable_daily_signal_report=False,
            strongest_count=3,
            weakest_count=1,
            min_abs_score=0.45,
            volume_ratio_min=0.80,
            stop_atr=1.20,
            target_r=2.0,
            initial_budget_eur=10.0,
            min_net_target_eur=0.10,
            max_hold_candles=48,
        )
        self.personal_monitor = PersonalIntradayMonitor(
            public_monitor.postgres,
            public_monitor.event_bus,
            ambiguous_candle_mode=public_monitor.ambiguous_candle_mode,
            max_shadow_signals_per_cycle=50,
            initial_budget_eur=10.0,
            sell_fee_rate=public_monitor.sell_fee_rate,
            spread_rate=public_monitor.spread_rate,
            slippage_rate=public_monitor.slippage_rate,
        )
        self.logger.info(
            "DUAL_PORTFOLIO enabled public=relative_strength private=spot_long_06_00_12h budget=10EUR"
        )

    @staticmethod
    def _is_private_decision_time() -> bool:
        now = datetime.now(ROME)
        return now.hour == 6 and now.minute < 15

    def _run_private_cycle(self, *, data_ready: bool) -> None:
        try:
            self.personal_monitor.monitor_open_signals()
            if data_ready and self._is_private_decision_time():
                self.personal_scanner.evaluate()
        except Exception:
            self.logger.exception("PRIVATE_PORTFOLIO_CYCLE failed")

    def _run_startup_cycle(self) -> None:
        super()._run_startup_cycle()
        self._run_private_cycle(data_ready=True)

    def _run_data_collector_sync(self, timeframes: list[str]) -> None:
        # Reimplement the parent cycle so both monitors use the same freshly synced candles.
        try:
            results = self.data_collector.sync_all_pairs(
                self.settings.collector_pairs,
                list(dict.fromkeys(timeframes)),
            )
            successful = [
                item for item in results if getattr(item, "status", None) == "SUCCESS"
            ]
            ok15 = any(getattr(item, "timeframe", "") == "15m" for item in successful)
            ok1h = any(getattr(item, "timeframe", "") == "1h" for item in successful)
            if ok15 and self.position_monitor is not None:
                self.position_monitor.monitor_open_signals()
            if ok15:
                self.personal_monitor.monitor_open_signals()
            if ok15 and ok1h and self.strategy_engine is not None:
                self.strategy_engine.evaluate()
                if self._is_private_decision_time():
                    self.personal_scanner.evaluate()
        except Exception:
            self.logger.exception("DUAL_PORTFOLIO_CYCLE failed")
        finally:
            from project.shared.memory import release_unused_memory
            release_unused_memory()
            self._collector_lock.release()
