"""A webhook alert is only ever posted to http(s).

The dispatcher POSTs with urlopen, which also speaks file:// and custom
schemes. A configured URL that names anything else is refused before any
network call is made.
"""
from __future__ import annotations

from threefold.application.webhook_notifier import WebhookConfig, WebhookNotifier
from threefold.domain.events import DomainEvent


def _event() -> DomainEvent:
    return DomainEvent.create("SECRET_DETECTED", "session-acme-1", {"path": "src/settings.py"})


def test_a_disabled_notifier_records_the_alert_and_sends_nothing() -> None:
    notifier = WebhookNotifier(WebhookConfig(webhook_url="https://hooks.acme.example/x", enabled=False))
    assert notifier.dispatch(_event()) is True
    assert len(notifier.sent_alerts) == 1


def test_a_missing_url_records_the_alert_and_sends_nothing() -> None:
    notifier = WebhookNotifier(WebhookConfig())
    assert notifier.dispatch(_event()) is True
    assert len(notifier.sent_alerts) == 1


def test_a_file_url_is_refused() -> None:
    notifier = WebhookNotifier(WebhookConfig(webhook_url="file:///etc/passwd"))
    assert notifier.dispatch(_event()) is False


def test_a_custom_scheme_is_refused() -> None:
    notifier = WebhookNotifier(WebhookConfig(webhook_url="gopher://acme.example/hook"))
    assert notifier.dispatch(_event()) is False
