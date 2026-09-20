"""AWS CloudWatch Embedded Metric Format (EMF) Logger for Threefold.

Emits structured JSON to stdout compliant with AWS CloudWatch EMF specification.
Enables real-time operational monitoring of AI agent tool execution, cost spend,
loop thrashing, and secret leak intercepts.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, Optional


def emit_threefold_emf_metrics(
    metrics: Dict[str, float],
    dimensions: Optional[Dict[str, str]] = None,
    namespace: str = "Threefold/Governance",
    extra_properties: Optional[Dict[str, Any]] = None,
) -> None:
    """Emits an AWS CloudWatch EMF record to stdout."""
    dim_dict = dimensions or {"Project": "Acme-Core", "Environment": "Production"}
    dim_keys = list(dim_dict.keys())

    metric_definitions = []
    for metric_name in metrics.keys():
        unit = "Count"
        if "USD" in metric_name or "Cost" in metric_name:
            unit = "None"
        elif "Ms" in metric_name or "Latency" in metric_name:
            unit = "Milliseconds"
        elif "Tokens" in metric_name:
            unit = "Count"

        metric_definitions.append({"Name": metric_name, "Unit": unit})

    emf_payload: Dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [dim_keys],
                    "Metrics": metric_definitions,
                }
            ],
        },
    }

    for k, v in dim_dict.items():
        emf_payload[k] = v

    for k, v in metrics.items():
        emf_payload[k] = v

    if extra_properties:
        for k, v in extra_properties.items():
            emf_payload[k] = v

    try:
        sys.stdout.write(json.dumps(emf_payload) + "\n")
        sys.stdout.flush()
    except (OSError, IOError):
        pass
