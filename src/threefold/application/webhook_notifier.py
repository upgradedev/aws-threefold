"""Enterprise Webhook Alert Dispatcher for Threefold.

Formats and dispatches real-time security alerts to enterprise incident response
channels (Slack, PagerDuty, or SOC webhook endpoints) when agents breach safety invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Dict, List, Optional
import urllib.request

from threefold.domain.events import DomainEvent, DomainEventPublisher

logger = logging.getLogger("threefold.webhooks")


@dataclass
class WebhookConfig:
    webhook_url: Optional[str] = None
    service_name: str = "Threefold Governance"
    enabled: bool = True


class WebhookNotifier:
    """Dispatches formatted alert cards to enterprise security webhooks."""

    def __init__(self, config: Optional[WebhookConfig] = None) -> None:
        self.config = config or WebhookConfig()
        self.sent_alerts: List[Dict[str, Any]] = []

    def format_slack_card(self, event: DomainEvent) -> Dict[str, Any]:
        """Formats a Slack-compatible Block Kit alert message."""
        severity_color = "#FF0000" if "SECRET" in event.event_type or "TERMINATED" in event.event_type else "#FFA500"
        return {
            "attachments": [
                {
                    "color": severity_color,
                    "title": f"🚨 [Threefold Alert] {event.event_type}",
                    "fields": [
                        {"title": "Session ID", "value": event.aggregate_id, "short": True},
                        {"title": "Timestamp UTC", "value": event.occurred_at_utc, "short": True},
                        {"title": "Payload Details", "value": json.dumps(event.payload, indent=2), "short": False},
                    ],
                    "footer": "Threefold Zero-Trust Governance Gatekeeper",
                }
            ]
        }

    def dispatch(self, event: DomainEvent) -> bool:
        """Dispatches event to configured webhook. Records alert in history."""
        payload = self.format_slack_card(event)
        self.sent_alerts.append({
            "event_id": event.event_id,
            "event_type": event.event_type,
            "aggregate_id": event.aggregate_id,
            "payload": payload,
        })

        if not self.config.enabled or not self.config.webhook_url:
            return True

        try:
            req = urllib.request.Request(
                self.config.webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status in (200, 201, 204)
        except Exception as exc:
            logger.warning("Failed to deliver webhook alert: %s", exc)
            return False


# Global singleton instance
global_webhook_notifier = WebhookNotifier()

# Auto-subscribe to all critical domain events
DomainEventPublisher.subscribe("*", global_webhook_notifier.dispatch)
