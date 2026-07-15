"""Internal event bus primitives for decoupled platform modules."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable


class EventType(StrEnum):
    MARKET_UPDATED = "MARKET_UPDATED"
    RESEARCH_COMPLETED = "RESEARCH_COMPLETED"
    MARKET_REGIME_CHANGED = "MARKET_REGIME_CHANGED"
    STRATEGY_VALIDATED = "STRATEGY_VALIDATED"
    NEW_SIGNAL = "NEW_SIGNAL"
    WATCHLIST = "WATCHLIST"
    TRADE_OPENED = "TRADE_OPENED"
    TRADE_CLOSED = "TRADE_CLOSED"
    STOP_LOSS = "STOP_LOSS"
    TARGET_HIT = "TARGET_HIT"
    REPORT_READY = "REPORT_READY"


@dataclass(frozen=True)
class Event:
    type: EventType
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


EventHandler = Callable[[Event], None]


class EventBus:
    """Small synchronous in-process event bus.

    Modules publish events instead of calling one another directly. A later phase can
    replace this implementation with Redis, Postgres LISTEN/NOTIFY, or a queue.
    """

    def __init__(self) -> None:
        self._subscribers: dict[EventType, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        for handler in self._subscribers.get(event.type, []):
            handler(event)
