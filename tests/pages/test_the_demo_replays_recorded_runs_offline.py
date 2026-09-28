"""The demo's offline panels replay recorded runs, not invented numbers.

When the service cannot be reached, each scenario replays one real run
recorded against the live API (the RECORDED block in index.html): the
statuses, reasons, costs and the certificate below all came out of the
service on the recorded date. The panels say "recorded" so a replay is
never mistaken for a run just made, and the replay shows no fix: only a
live answer carries one.

The pages' own scripts run under Node with the stub browser in _browser.py;
these tests skip where Node is absent.
"""
from __future__ import annotations

import html
from pathlib import Path

from _browser import run

# The demo page writes its terminal log with createElement and appendChild,
# which the shared stub browser does not provide; the log is not under test.
DEMO_DOM = r"""
document.createElement = tag => ({ tagName: tag, className: '', textContent: '', innerHTML: '', setAttribute() {}, click() {}, remove() {} });
el('terminal-log').appendChild = () => {};
"""

OFFLINE = "answer = () => 'network'; await checkApiHealth();"


def _page(scenario: str, tmp_path: Path) -> dict:
    return run("index.html", scenario, tmp_path, before=DEMO_DOM)


def test_the_loop_replay_shows_the_recorded_refusal(tmp_path: Path) -> None:
    out = _page(
        OFFLINE + r"""
  await simulateLoop(); await tick();
  out.verdict = el('verdict-tag').innerText;
  out.explained = el('bedrock-box').innerHTML;
  out.loopBadge = el('inv-loop').innerText;
  out.spend = el('kpi-spend').innerText;
  out.note = el('kpi-note').textContent;
  out.noteHidden = el('kpi-note').hidden;
""",
        tmp_path,
    )
    assert out["verdict"] == "BLOCKED_LOOP_DETECTED"
    assert out["loopBadge"] == "FAIL (Loop)"
    assert "Recorded 2026-09-25, replayed offline" in out["explained"]
    assert "Monomorphic loop detected" in out["explained"]
    assert "$0.0270" in out["explained"]
    assert "3 consecutive times. The recorded session’s projected spend was $0.0270." in out["explained"], \
        "A review read the recorded reason and the cost as one sentence: the reason ends in a stop of its own"
    assert out["spend"] == "$0.0270", "The recorded session's projected cost is its spend, as the certificate's replay shows its own"
    assert out["note"].startswith("Projected from the token counts this demo declares") and not out["noteHidden"], \
        "A judge: the spend is projected from declared token counts, never metered, and says so"


def test_the_secret_replay_shows_the_recorded_reason(tmp_path: Path) -> None:
    out = _page(
        OFFLINE + r"""
  await simulateSecret(); await tick();
  out.verdict = el('verdict-tag').innerText;
  out.explained = el('bedrock-box').innerHTML;
""",
        tmp_path,
    )
    assert out["verdict"] == "BLOCKED_SECRET_DETECTED"
    assert "Recorded 2026-09-25, replayed offline" in out["explained"]
    assert "Sensitive credential detected: AWS_ACCESS_KEY. The recorded run quarantined it before transmission." in out["explained"]


def test_the_boundary_replay_shows_the_recorded_rule(tmp_path: Path) -> None:
    out = _page(
        OFFLINE + r"""
  await simulateBoundary(); await tick();
  out.verdict = el('verdict-tag').innerText;
  out.explained = el('bedrock-box').innerHTML;
""",
        tmp_path,
    )
    assert out["verdict"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert "Recorded 2026-09-25, replayed offline" in out["explained"]
    assert "python-domain-stays-pure" in out["explained"]
    assert html.unescape(out["explained"]).endswith("which matches 'boto3'."), "The recorded reason ends in a stop"


def test_a_recorded_reason_is_given_a_stop_only_where_it_has_none(tmp_path: Path) -> None:
    out = _page(
        r"""
  out.said = ['Refused.', 'Why?', "which matches 'boto3'", '  AWS_ACCESS_KEY  ', 'It said "stop."', 'Halted (3 calls).'].map(asSentence);
""",
        tmp_path,
    )
    assert out["said"] == ["Refused.", "Why?", "which matches 'boto3'.", "AWS_ACCESS_KEY.", 'It said "stop."', "Halted (3 calls)."]


def test_the_compliant_replay_shows_the_recorded_certificate(tmp_path: Path) -> None:
    out = _page(
        OFFLINE + r"""
  await simulateCompliant(); await tick();
  out.spend = el('kpi-spend').innerText;
  out.tokens = el('kpi-tokens').innerText;
  out.note = el('kpi-note').textContent;
  out.explained = el('bedrock-box').innerHTML;
  out.certId = el('cert-id').innerText;
  out.certHash = el('cert-hash').innerText;
  out.certOrigin = el('cert-origin').innerHTML;
  out.fixShown = !el('fix-container').classList.contains('hidden');
""",
        tmp_path,
    )
    assert out["spend"] == "$0.0384"
    # A judge: "6400 tokens consumed" read as tokens a model used. They are the
    # counts the demo declares for its four calls, 1,200 in and 400 out each.
    assert out["tokens"] == "6,400 projected tokens"
    assert out["note"] == ("Projected from the token counts this demo declares, 1,200 input and 400 output per call; "
                           "approved calls only, nothing metered.")
    assert "Four approved calls: $0.0384 projected for the 6,400 tokens this demo declares." in out["explained"]
    assert out["certId"] == "CERT-TF-38EC0B01"
    assert "32c95977d310b51080bdae0c99bd2f93cf7608c87e652f082b8626fd159cd4b6" in out["certHash"]
    assert "Recorded 2026-09-25, replayed offline" in out["certOrigin"]
    assert not out["fixShown"], "The replay shows no fix"
