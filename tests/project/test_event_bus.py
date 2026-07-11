from project.shared.events import Event, EventBus, EventType


def test_event_bus_delivers_subscribed_events():
    bus = EventBus()
    received = []
    bus.subscribe(EventType.MARKET_UPDATED, received.append)

    event = Event(EventType.MARKET_UPDATED, {"pair": "BTC/USD"})
    bus.publish(event)

    assert received == [event]
