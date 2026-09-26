"""Self-correction is worded against the refusals that had a chance to be corrected.

Since 2026-09-25 the service takes the rate over the refusals whose session made
a later call, and counts the single-call ones (live probes, smoke tests) apart.
The page must say which number the rate is of: shown against every refusal, a
rate of three that went on would read as a rate of all twenty-two. A figure from
a stack that predates the count is worded as before. Run under Node with the stub
browser in _browser.py, as the other page tests are.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

from pathlib import Path

from test_the_proof_page import proof


def test_the_rate_is_worded_against_the_refusals_followed_by_another_call(tmp_path: Path) -> None:
    out = proof(
        r"""
  const figure = { refusals_considered: 22, refusals_with_later_call: 3, refusals_without_later_call: 19, self_corrected: 3,
                   rate: 1, median_calls_to_correct: 2, rows_read: 900, complete: true };
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: figure }) } });
  await visit('#/overview?days=7');
  out.view = view();
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    assert out["metrics"]["self_corrected"] == "3"
    assert "100% of 3 agent refusals followed by another call" in out["view"]
    assert "19 more had no later call" in out["view"] and "median 2 calls" in out["view"]
    assert "of 22 agent refusals" not in out["view"], "The rate is never shown against refusals that had no chance"


def test_a_window_where_no_refusal_had_a_later_call_shows_no_figure(tmp_path: Path) -> None:
    out = proof(
        r"""
  const none = { refusals_considered: 22, refusals_with_later_call: 0, refusals_without_later_call: 22, self_corrected: 0,
                 rate: null, median_calls_to_correct: null, rows_read: 400, complete: true };
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: none }) } });
  await visit('#/overview?days=7');
  out.many = view();
  out.manyMetrics = metrics(view());
  const one = Object.assign({}, none, { refusals_considered: 1, refusals_without_later_call: 1 });
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: one }) } });
  await visit('#/overview?days=14');
  out.one = view();
""",
        tmp_path,
    )
    assert out["manyMetrics"]["self_corrected"] == "—", "Nothing had a chance, so no zero that reads as a failure"
    assert "not measured: none of the 22 agent refusals had a later call in its session" in out["many"]
    assert "0% of" not in out["many"]
    assert "not measured: the one agent refusal had no later call in its session" in out["one"]


def test_the_project_page_uses_the_same_words(tmp_path: Path) -> None:
    out = proof(
        r"""
  const detail = detailBody('enforce');
  detail.readiness.summary.self_correction = { refusals_considered: 5, refusals_with_later_call: 4, refusals_without_later_call: 1,
    self_corrected: 3, rate: 0.75, median_calls_to_correct: 1, rows_read: 70, complete: true };
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: detail } });
  await visit('#/projects/Acme-Billing?days=14');
  out.view = view();
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    assert out["metrics"]["self_corrected"] == "3"
    assert "75% of 4 agent refusals followed by another call" in out["view"] and "1 more had no later call" in out["view"]


def test_the_proof_page_words_a_snapshot_with_the_counts_the_same_way(tmp_path: Path) -> None:
    out = proof(
        r"""
  const snapshot = JSON.parse(JSON.stringify(MEASURED));
  snapshot.private.self_correction = { refusals_considered: 6, refusals_with_later_call: 5, refusals_without_later_call: 1,
    self_corrected: 4, rate: 0.8, median_calls_to_correct: 1, rows_read: 1900, complete: true };
  answer = proofAnswer(snapshot);
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    assert "80% of 5 agent refusals followed by another call" in out["view"] and "1 more had no later call" in out["view"]
