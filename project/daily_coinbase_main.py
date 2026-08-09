"""Entrypoint for the daily Coinbase PAPER signal runtime."""
from __future__ import annotations

from project.daily_coinbase_runtime import DailyCoinbaseRuntime


def main() -> None:
    runtime = DailyCoinbaseRuntime()
    runtime.postgres.execute(
        """UPDATE signals.generated_signals
           SET status='EXPIRED',
               closed_at=COALESCE(closed_at, NOW()),
               outcome_resolution='REPLACED_BY_DAILY_COINBASE_13H_RUNTIME'
           WHERE status IN ('NEW','OPEN')"""
    )
    runtime.logger.info('LEGACY_OPEN_SIGNALS_RETIRED_FOR_DAILY_COINBASE_RUNTIME')
    runtime.run_forever()


if __name__ == '__main__':
    main()
