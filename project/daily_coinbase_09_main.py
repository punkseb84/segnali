"""Start the existing daily BUY/SELL PAPER scanner at 09:00 Europe/Rome."""
from __future__ import annotations

import project.daily_coinbase_runtime as base_runtime

base_runtime.ANALYSIS_HOUR = 9

from project.daily_coinbase_buy_sell_runtime import DailyCoinbaseBuySellRuntime


def main() -> None:
    runtime = DailyCoinbaseBuySellRuntime()
    runtime.postgres.execute(
        """UPDATE signals.generated_signals
           SET status='EXPIRED',
               closed_at=COALESCE(closed_at, NOW()),
               outcome_resolution='REPLACED_BY_DAILY_BUY_SELL_09H_RUNTIME'
           WHERE status IN ('NEW','OPEN')"""
    )
    runtime.logger.info('DAILY_BUY_SELL_SCHEDULE analysis=09:00 timezone=Europe/Rome')
    runtime.run_forever()


if __name__ == '__main__':
    main()
