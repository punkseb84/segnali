"""Notification Engine: dispatch events to Telegram/Discord/Email/Webhook adapters."""
from project.notification_engine.service import NotificationEngine

__all__ = ["NotificationEngine"]
