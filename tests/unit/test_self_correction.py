"""Self-correction: a refused agent that went on to do the same thing acceptably.

A refusal of a hook or CI call self-corrected when one of the next ten calls of
its own session aimed at the same target and was not refused. The definition
is driven here on crafted sessions, with no store at all; the bounded read that
feeds it is driven with a reader of fixed rows, paged the way the ledger pages,
so the incomplete flag can be made to trip on purpose.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime

import pytest

from threefold.application import insights, ledger

TODAY = datetime.date(2026, 9, 22)
DOMAIN = "src/acme/domain/order.py"
ADAPTER = "src/acme/infrastructure/archive.py"
REFUSED = "BLOCKED_BOUNDARY_VIOLATION"


def session(*calls, session_id="acme-session-1", origin="hook", day="2026-09-22"):
    """Rows of one session, one a second apart, from (status, target) pairs."""
    rows = []
    for index, (status, target) in enumerate(calls):
        rows.append({
            "verdict_id": f"{session_id}-{index}",
            "timestamp": f"{day}T10:{index // 60:02d}:{index % 60:02d}+00:00",
            "session_id": session_id,
            "origin": origin,
            "project_name": "Acme-Orders",
            "status": status,
            "target": target,
        })
    return rows


def figure(rows):
    return insights.self_correction(rows)


# ---------------------------------------------------------------- the definition


def test_a_refusal_followed_by_an_approval_on_the_same_target_is_corrected() -> None:
    result = figure(session((REFUSED, DOMAIN), ("APPROVED", ADAPTER), ("APPROVED", DOMAIN)))
    assert result == {"refusals_considered": 1, "refusals_with_later_call": 1, "refusals_without_later_call": 0,
                      "self_corrected": 1, "rate": 1.0, "median_calls_to_correct": 2}


def test_a_refusal_never_followed_by_an_acceptable_call_is_not_corrected() -> None:
    result = figure(session((REFUSED, DOMAIN), (REFUSED, DOMAIN), ("APPROVED", "README.md")))
    assert (result["refusals_considered"], result["self_corrected"], result["rate"]) == (2, 0, 0.0)
    assert result["refusals_with_later_call"] == 2, "Both had a later call, so both had a chance and missed it"
    assert result["median_calls_to_correct"] is None, "No correction has no median; zero would read as at once"


def test_the_tenth_call_after_counts_and_the_eleventh_does_not() -> None:
    filler = [("APPROVED", f"src/acme/app/step_{index}.py") for index in range(9)]
    within = figure(session((REFUSED, DOMAIN), *filler, ("APPROVED", DOMAIN)))
    assert (within["self_corrected"], within["median_calls_to_correct"]) == (1, 10)
    beyond = figure(session((REFUSED, DOMAIN), *filler, ("APPROVED", ADAPTER), ("APPROVED", DOMAIN)))
    assert (beyond["refusals_considered"], beyond["self_corrected"]) == (1, 0)


def test_an_approval_on_another_target_is_not_a_correction() -> None:
    result = figure(session((REFUSED, DOMAIN), ("APPROVED", ADAPTER), ("APPROVED", "src/acme/app/service.py")))
    assert (result["refusals_considered"], result["self_corrected"]) == (1, 0)


def test_an_approval_in_another_session_is_not_a_correction() -> None:
    rows = session((REFUSED, DOMAIN)) + session(("APPROVED", DOMAIN), session_id="acme-session-2")
    assert figure(rows)["self_corrected"] == 0


def test_the_observe_stage_refuses_nothing_so_it_considers_nothing() -> None:
    watched = [dict(row, observed_rules=["python-domain-stays-pure"], stage="observe", dry_run=True)
               for row in session(("APPROVED", DOMAIN), ("APPROVED", DOMAIN))]
    result = figure(watched)
    assert result == {"refusals_considered": 0, "refusals_with_later_call": 0, "refusals_without_later_call": 0,
                      "self_corrected": 0, "rate": None, "median_calls_to_correct": None}


def test_a_later_call_that_is_only_observed_is_a_correction() -> None:
    rows = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN))
    rows[1].update(observed_rules=["python-domain-stays-pure"], observed_rule="python-domain-stays-pure")
    assert figure(rows)["self_corrected"] == 1


def test_only_hook_and_ci_refusals_are_considered() -> None:
    page = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), session_id="sim-demo", origin="page")
    ci = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), session_id="acme-ci", origin="ci")
    unknown = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), session_id="acme-old", origin="unknown")
    result = figure(page + ci + unknown)
    assert (result["refusals_considered"], result["self_corrected"]) == (1, 1)


def test_a_refusal_with_no_recorded_target_is_not_considered() -> None:
    result = figure(session((REFUSED, ""), ("APPROVED", ""), (REFUSED, DOMAIN)))
    # The one refusal considered is the session's last call: it had no chance
    # to be corrected, so the rate is of nothing rather than 0%.
    assert (result["refusals_considered"], result["self_corrected"], result["rate"]) == (1, 0, None)
    assert (result["refusals_with_later_call"], result["refusals_without_later_call"]) == (0, 1)


def test_calls_are_ordered_in_time_whatever_order_they_are_read_in() -> None:
    rows = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN))
    assert figure(list(reversed(rows)))["self_corrected"] == 1
    # And an approval before the refusal is not an answer to it.
    earlier = session(("APPROVED", DOMAIN), (REFUSED, DOMAIN))
    assert figure(earlier)["self_corrected"] == 0


def test_each_refusal_counts_and_the_median_is_of_the_distances() -> None:
    rows = session((REFUSED, DOMAIN), (REFUSED, DOMAIN), ("APPROVED", DOMAIN), ("BLOCKED_LOOP_DETECTED", "npm"))
    result = figure(rows)
    assert (result["refusals_considered"], result["self_corrected"]) == (3, 2)
    # The refusal of `npm` is the session's last call, so it had no chance and
    # the rate is over the two that did.
    assert (result["refusals_with_later_call"], result["refusals_without_later_call"]) == (2, 1)
    assert result["rate"] == 1.0 and result["median_calls_to_correct"] == 1.5


def test_a_path_written_with_backslashes_or_a_leading_dot_is_the_same_target() -> None:
    rows = session((REFUSED, "src\\acme\\domain\\order.py"), ("APPROVED", "./" + DOMAIN))
    assert figure(rows)["self_corrected"] == 1


def test_a_row_read_twice_counts_once() -> None:
    rows = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN))
    assert figure(rows + [dict(rows[0])])["refusals_considered"] == 1


# ---------------------------------------------------------------- only a refusal with a later call had a chance


def test_a_refusal_whose_session_made_no_later_call_is_counted_apart_and_not_in_the_rate() -> None:
    # A live probe or a smoke test: one call, refused, and the session ends.
    result = figure(session((REFUSED, DOMAIN), session_id="probe-0a1b2c3d-layering"))
    assert result == {"refusals_considered": 1, "refusals_with_later_call": 0, "refusals_without_later_call": 1,
                      "self_corrected": 0, "rate": None, "median_calls_to_correct": None}


def test_when_no_refusal_had_a_later_call_the_rate_is_null_not_zero() -> None:
    single_shots = []
    for index, prefix in enumerate(("probe", "smoke", "probe", "smoke", "acme-agent")):
        single_shots += session((REFUSED, DOMAIN), session_id=f"{prefix}-{index}")
    result = figure(single_shots)
    assert (result["refusals_considered"], result["refusals_without_later_call"]) == (5, 5)
    assert result["refusals_with_later_call"] == 0 and result["self_corrected"] == 0
    assert result["rate"] is None, "Nothing had a chance to correct itself, so nothing was measured: not 0%"


def test_single_call_sessions_leave_the_rate_of_the_agents_that_went_on() -> None:
    probes = [row for index in range(20) for row in session((REFUSED, DOMAIN), session_id=f"probe-{index:02d}")]
    agent = session((REFUSED, DOMAIN), ("APPROVED", ADAPTER), ("APPROVED", DOMAIN), session_id="acme-agent")
    walked_on = session((REFUSED, DOMAIN), ("APPROVED", "README.md"), session_id="acme-agent-2")
    result = figure(probes + agent + walked_on)
    assert result["refusals_considered"] == 22, "refusals_considered keeps its meaning: every refusal considered"
    assert (result["refusals_with_later_call"], result["refusals_without_later_call"]) == (2, 20)
    assert (result["self_corrected"], result["rate"]) == (1, 0.5)


def test_a_refusal_corrected_within_ten_calls_had_its_chance_and_took_it() -> None:
    filler = [("APPROVED", f"src/acme/app/step_{index}.py") for index in range(9)]
    result = figure(session((REFUSED, DOMAIN), *filler, ("APPROVED", DOMAIN)))
    assert (result["refusals_with_later_call"], result["refusals_without_later_call"]) == (1, 0)
    assert (result["self_corrected"], result["rate"], result["median_calls_to_correct"]) == (1, 1.0, 10)


def test_a_refusal_corrected_only_on_the_eleventh_call_had_its_chance_and_missed_it() -> None:
    filler = [("APPROVED", f"src/acme/app/step_{index}.py") for index in range(10)]
    result = figure(session((REFUSED, DOMAIN), *filler, ("APPROVED", DOMAIN)))
    assert (result["refusals_with_later_call"], result["refusals_without_later_call"]) == (1, 0)
    assert (result["self_corrected"], result["rate"]) == (0, 0.0), "It had later calls, so a miss is 0%, not null"
    assert result["median_calls_to_correct"] is None


def test_any_later_call_is_a_chance_even_one_that_could_never_be_the_correction() -> None:
    # A later command, or the same refusal again, is the session going on:
    # the agent had a chance and did not take it within the window.
    command = session((REFUSED, DOMAIN), ("APPROVED", "pytest"))
    command[1]["action_type"] = "COMMAND_EXEC"
    assert (figure(command)["refusals_with_later_call"], figure(command)["rate"]) == (1, 0.0)
    again = figure(session((REFUSED, DOMAIN), (REFUSED, DOMAIN)))
    assert (again["refusals_with_later_call"], again["refusals_without_later_call"], again["rate"]) == (1, 1, 0.0)


def test_a_later_call_from_a_page_or_another_session_is_not_this_sessions_chance() -> None:
    refused = session((REFUSED, DOMAIN), session_id="acme-agent")
    page = session(("APPROVED", "README.md"), ("APPROVED", "README.md"), session_id="acme-agent", origin="page")
    other = session(("APPROVED", DOMAIN), ("APPROVED", DOMAIN), session_id="acme-agent-2")
    result = figure(refused + page + other)
    assert (result["refusals_with_later_call"], result["refusals_without_later_call"], result["rate"]) == (0, 1, None)


def test_with_and_without_a_later_call_always_add_up_to_the_refusals_considered() -> None:
    rows = (session((REFUSED, DOMAIN), (REFUSED, ADAPTER), ("APPROVED", DOMAIN), (REFUSED, "src/acme/web/cart.ts"))
            + session((REFUSED, DOMAIN), session_id="smoke-1")
            + session((REFUSED, ""), ("APPROVED", DOMAIN), session_id="acme-agent-3"))
    result = figure(rows)
    assert result["refusals_with_later_call"] + result["refusals_without_later_call"] == result["refusals_considered"] == 4
    assert (result["refusals_with_later_call"], result["self_corrected"], result["rate"]) == (2, 1, 0.5)



# ---------------------------------------------------------------- calls that share an instant


def _at(stamp: str, status: str, target: str, verdict_id: str, action: str = "FILE_WRITE") -> dict:
    return {"verdict_id": verdict_id, "timestamp": f"2026-09-22T10:00:00.{stamp}+00:00", "session_id": "acme-session-1",
            "origin": "hook", "project_name": "Acme-Orders", "status": status, "target": target, "action_type": action}


@pytest.mark.parametrize("read_id, write_id", [("VERDICT-000001", "VERDICT-FFFFFF"), ("VERDICT-FFFFFF", "VERDICT-000001")])
def test_calls_that_share_an_instant_are_never_ordered_by_their_random_ids(read_id: str, write_id: str) -> None:
    # On a clock that ticks once a millisecond, the read and the clean write
    # after a refusal can share a timestamp. Which id sorts first is chance,
    # so the answer must be the same either way: the write may have been the
    # second call, and a tie never makes it look sooner than that.
    rows = [_at("053932", REFUSED, DOMAIN, "VERDICT-AAAAAA"),
            _at("054931", "APPROVED", "README.md", read_id, "FILE_READ"),
            _at("054931", "APPROVED", DOMAIN, write_id)]
    for ordering in (rows, list(reversed(rows)), [rows[2], rows[0], rows[1]]):
        result = figure(ordering)
        assert (result["self_corrected"], result["median_calls_to_correct"]) == (1, 2)


@pytest.mark.parametrize("read_id", ["VERDICT-000001", "VERDICT-FFFFFF"])
def test_a_call_at_the_refusals_own_instant_counts_as_in_between_but_never_as_its_answer(read_id: str) -> None:
    shared = [_at("066617", REFUSED, DOMAIN, "VERDICT-888888"), _at("066617", "APPROVED", "README.md", read_id, "FILE_READ"),
              _at("067616", "APPROVED", DOMAIN, "VERDICT-123456")]
    assert (figure(shared)["self_corrected"], figure(shared)["median_calls_to_correct"]) == (1, 2)
    # An approval at the very instant of the refusal may have come before it.
    same_instant = [_at("066617", REFUSED, DOMAIN, "VERDICT-888888"), _at("066617", "APPROVED", DOMAIN, read_id)]
    assert (figure(same_instant)["refusals_considered"], figure(same_instant)["self_corrected"]) == (1, 0)
    # Nor is it a later call: it gave the refusal no known chance, so the rate
    # is of nothing rather than a miss.
    assert (figure(same_instant)["refusals_without_later_call"], figure(same_instant)["rate"]) == (1, None)


def test_a_tie_at_the_edge_of_the_window_does_not_stretch_it() -> None:
    refusal = _at("000000", REFUSED, DOMAIN, "VERDICT-0")
    filler = [_at(f"{index:06d}", "APPROVED", f"src/acme/app/step_{index}.py", f"VERDICT-{index}") for index in range(1, 10)]
    # The tenth and eleventh calls share an instant: the correction may have
    # been the eleventh, so it is not counted inside a window of ten.
    tied = [_at("000010", "APPROVED", DOMAIN, "VERDICT-10"), _at("000010", "APPROVED", ADAPTER, "VERDICT-11")]
    assert figure([refusal] + filler + tied)["self_corrected"] == 0
    assert figure([refusal] + filler + tied[:1])["median_calls_to_correct"] == 10


# ---------------------------------------------------------------- commands


def test_a_refused_command_is_not_considered_because_its_target_is_only_a_program() -> None:
    rows = session((REFUSED, "git"), ("APPROVED", "git"))
    for row in rows:
        row["action_type"] = "COMMAND_EXEC"
    # A refused `git commit --no-verify` followed by `git status` is not an
    # agent correcting itself; the ledger cannot tell them apart.
    assert figure(rows) == {"refusals_considered": 0, "refusals_with_later_call": 0, "refusals_without_later_call": 0,
                            "self_corrected": 0, "rate": None, "median_calls_to_correct": None}


def test_a_refused_shell_write_is_left_out_rather_than_scored_as_never_corrected() -> None:
    rows = session((REFUSED, "cat"), ("APPROVED", DOMAIN))
    rows[0]["action_type"] = "COMMAND_EXEC"
    rows[1]["action_type"] = "FILE_WRITE"
    result = figure(rows)
    assert (result["refusals_considered"], result["rate"]) == (0, None)


def test_a_command_is_never_a_correction_but_is_one_of_the_calls_in_between() -> None:
    script = "scripts/deploy.sh"
    rows = session((REFUSED, script), ("APPROVED", "./" + script), ("APPROVED", script))
    rows[1]["action_type"] = "COMMAND_EXEC"
    result = figure(rows)
    assert (result["self_corrected"], result["median_calls_to_correct"]) == (1, 2)
    assert figure(rows[:2])["self_corrected"] == 0, "Running the script is not writing it acceptably"


# ---------------------------------------------------------------- the bounded read


class _Reader:
    """A ledger of fixed rows, newest first within a day, paged the way read_decision_day pages."""

    def __init__(self, rows):
        self.by_day = {}
        for row in rows:
            keyed = dict(row, _sk=f"{row['timestamp']}#{row['verdict_id']}")
            self.by_day.setdefault(row["timestamp"][:10], []).append(keyed)
        for day_rows in self.by_day.values():
            day_rows.sort(key=lambda row: row["_sk"], reverse=True)
        self.reads = []

    def __call__(self, day, after, limit):
        self.reads.append((day, after, limit))
        rows = [row for row in self.by_day.get(day, []) if after is None or row["_sk"] < after]
        page = rows[:limit]
        return page, (page[-1]["_sk"] if len(rows) > len(page) else None)


def test_the_whole_window_read_is_complete(monkeypatch) -> None:
    monkeypatch.setattr(ledger, "PAGE_SIZE", 2)
    rows = (session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), day="2026-09-22")
            + session((REFUSED, ADAPTER), ("APPROVED", "README.md"), session_id="acme-session-2", day="2026-09-20"))
    result = ledger.self_correction(_Reader(rows), days=7, today=TODAY)
    assert result == {"refusals_considered": 2, "refusals_with_later_call": 2, "refusals_without_later_call": 0,
                      "self_corrected": 1, "rate": 0.5, "median_calls_to_correct": 1, "rows_read": 4, "complete": True}


def test_a_read_that_runs_out_of_budget_says_it_is_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(ledger, "PAGE_SIZE", 2)
    older = session((REFUSED, ADAPTER), ("APPROVED", ADAPTER), session_id="acme-session-2", day="2026-09-21")
    newest = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), ("APPROVED", "README.md"), day="2026-09-22")
    reader = _Reader(older + newest)
    result = ledger.self_correction(reader, days=7, today=TODAY, budget=3)
    assert result["complete"] is False and result["rows_read"] == 3
    # What was read is the newest, and every refusal among it had its later calls read.
    assert (result["refusals_considered"], result["self_corrected"]) == (1, 1)
    assert all(limit <= 2 for _, _, limit in reader.reads) and sum(1 for _ in reader.reads) == 2


def test_a_quiet_month_is_read_to_the_end_because_the_budget_counts_rows() -> None:
    reader = _Reader(session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), day="2026-08-24"))
    result = ledger.self_correction(reader, days=30, today=TODAY, budget=5)
    assert result["complete"] is True and result["self_corrected"] == 1
    assert len(reader.reads) == 30, "One read per day of the window, empty or not"


def test_a_window_of_one_day_does_not_reach_yesterday() -> None:
    reader = _Reader(session((REFUSED, DOMAIN), ("APPROVED", DOMAIN), day="2026-09-21"))
    result = ledger.self_correction(reader, days=1, today=TODAY)
    assert (result["refusals_considered"], result["rows_read"], result["complete"]) == (0, 0, True)


def test_a_store_that_never_stops_answering_still_ends_incomplete() -> None:
    calls = []

    def endless(day, after, limit):
        calls.append(day)
        return [], "more"

    result = ledger.self_correction(endless, days=7, today=TODAY, budget=400)
    assert result["complete"] is False and result["rows_read"] == 0
    assert len(calls) < 20


def test_only_the_named_projects_rows_are_counted_and_every_row_is_reduced_first() -> None:
    mine = session((REFUSED, DOMAIN), ("APPROVED", DOMAIN))
    theirs = [dict(row, project_name="Acme-Catalog") for row in session((REFUSED, DOMAIN), session_id="acme-other")]
    raw_name = [dict(row, project_name="not an acme name") for row in session((REFUSED, DOMAIN), session_id="acme-raw")]
    rows, read, complete = ledger.read_window(_Reader(mine + theirs + raw_name), days=1, project="Acme-Orders", today=TODAY)
    assert read == 4 and complete and {row["project_name"] for row in rows} == {"Acme-Orders"}
    unlabelled, _, _ = ledger.read_window(_Reader(raw_name), days=1, project="unlabelled", today=TODAY)
    assert [row["project_name"] for row in unlabelled] == ["unlabelled"], "A raw name is read back as the bucket it is shown under"
    assert ledger.self_correction(_Reader(mine + theirs), days=1, project="Acme-Orders", today=TODAY)["self_corrected"] == 1


def test_the_figure_when_nothing_could_be_read() -> None:
    assert ledger.self_correction_unread() == {
        "refusals_considered": 0, "refusals_with_later_call": 0, "refusals_without_later_call": 0,
        "self_corrected": 0, "rate": None, "median_calls_to_correct": None, "rows_read": 0, "complete": False,
    }


@pytest.mark.parametrize("window", [1, 3])
def test_the_window_can_be_narrowed(window: int) -> None:
    rows = session((REFUSED, DOMAIN), ("APPROVED", ADAPTER), ("APPROVED", DOMAIN))
    expected = 1 if window >= 2 else 0
    assert insights.self_correction(rows, window=window)["self_corrected"] == expected
