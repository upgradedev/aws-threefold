"""Four measured matrices are committed; every document must say so, and say it right.

A review found the live proof page and six documents still telling a reader
that no benchmark result exists, while `benchmark/results/` held four runs of
the matrix and `docs/evidence/` their reports. The page was rebuilt (21b01bf);
the documents were not.

Every figure asserted here is read out of the run's own summary file, written
by `benchmark/report.py` beside the rows it aggregated, so a document that
drifts from the measurement fails rather than the test restating a number.
The two task families are reported apart and are never pooled, so each series
is checked on its own.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Each measured run of the matrix: its summary beside the rows, and the report.
SERIES = {
    "standard, claude-sonnet-5": (
        "20260922T143932Z-summary.json",
        "docs/evidence/BENCHMARK_2026-09-22.md",
    ),
    "standard, claude-haiku-4-5": (
        "20260922T145644Z-summary.json",
        "docs/evidence/BENCHMARK_2026-09-22-HAIKU.md",
    ),
    "pressure, claude-sonnet-5": (
        "20260922T161455Z-pressure-summary.json",
        "docs/evidence/BENCHMARK_2026-09-22-PRESSURE-SONNET.md",
    ),
    "pressure, claude-haiku-4-5": (
        "20260922T162306Z-pressure-summary.json",
        "docs/evidence/BENCHMARK_2026-09-22-PRESSURE-HAIKU.md",
    ),
}

# Every document a judge or a reader reaches that speaks about the benchmark.
DOCUMENTS = (
    "README.md",
    "docs/SUBMISSION_DOSSIER.md",
    "docs/BUILDER_CENTER_ARTICLE.md",
    "docs/VIDEO_SCRIPT.md",
    "docs/RUNBOOK.md",
)

# The documents that carry the whole matrix as a table a reader can compare.
TABLES = ("README.md", "docs/BUILDER_CENTER_ARTICLE.md")

CONDITIONS = ("none", "prompt", "threefold")

# What each document said before the measurement reached it, exactly as written
# there once its own line breaks are folded away.
STALE = (
    "No comparative benchmark number exists yet",
    "No benchmark result exists yet",
    "Only a pilot exists, and its real-agent runs never reached the model",
    "Only a pilot exists so far",
    "Only a pilot has run",
    "has only run as a pilot",
    "shows the benchmark as a pilot with nothing measured yet",
    "so there is no number to report",
    "so no result exists",
    "none is measured yet",
    "The headline will be computed",
    "its headline will come from the full",
    "Does Threefold change what an agent does?** Not measured yet",
)


def _text(name: str) -> str:
    """A document with its line breaks folded, so a phrase is found as written."""
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


def _measured() -> dict[str, dict[str, dict]]:
    """Each series' conditions, straight from the summary `report.py` wrote."""
    out: dict[str, dict[str, dict]] = {}
    for label, (summary, report) in SERIES.items():
        path = ROOT / "benchmark" / "results" / summary
        assert path.exists(), f"{summary} is missing, so {label} measured nothing"
        assert (ROOT / report).exists(), f"{report} is missing"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pilot"] is False, f"{summary} is a pilot and must not be quoted as a result"
        # A run of one family is summarised under that family; the standard
        # matrix, which is the default set, is summarised at the top level.
        families = data.get("families")
        if families:
            assert len(families) == 1, f"{summary} pools families, which the reports never do"
            data = next(iter(families.values()))
        agent = data["agents"]["claude-code"]
        assert agent["invalid_rows"] == 0, f"{summary} holds runs that measured nothing"
        out[label] = {name: agent["conditions"][name] for name in CONDITIONS}
    return out


def _share(k: int, n: int, rate: float) -> str:
    return f"{round(rate * 100)}% ({k}/{n})"


def test_the_four_series_are_measured_runs_of_the_matrix() -> None:
    measured = _measured()
    assert len(measured) == 4
    total = sum(c["n"] for series in measured.values() for c in series.values())
    assert total == 162, f"The documents speak of 162 runs; the summaries hold {total}"
    for label, series in measured.items():
        assert series["threefold"]["violations"] == 0, f"{label}: a violation landed under Threefold"


def test_no_document_still_says_the_benchmark_measured_nothing() -> None:
    for name in DOCUMENTS:
        body = _text(name)
        for phrase in STALE:
            assert phrase not in body, f"{name} still says: {phrase!r}"


def test_every_document_carries_each_series_own_rates() -> None:
    """The rates that tell the series apart, in every document that names them.

    A document quoting one series and calling it "the benchmark" would pool
    what the reports refuse to pool, so each series' unguided rate and its
    rate with the rules in `CLAUDE.md` alone must be there.
    """
    measured = _measured()
    for name in DOCUMENTS:
        body = _text(name)
        for label, series in measured.items():
            for condition in ("none", "prompt"):
                percent = f"{round(series[condition]['violation_rate'] * 100)}%"
                assert percent in body, f"{name} does not carry {label}, {condition} = {percent}"


def test_the_tables_state_every_cell_as_the_summaries_have_it() -> None:
    measured = _measured()
    for name in TABLES:
        body = _text(name)
        for label, series in measured.items():
            for condition in CONDITIONS:
                cell = series[condition]
                share = _share(cell["violations"], cell["n"], cell["violation_rate"])
                assert share in body, f"{name} does not state {label}, {condition} violations as {share}"
            done = series["threefold"]
            passed = _share(done["completions"], done["n"], done["completion_rate"])
            assert passed in body, f"{name} does not state {label} tests passed under Threefold as {passed}"


def test_every_document_states_what_enforcing_cost_under_the_pressure_prompts() -> None:
    """The refusals stopped the agent as well as the violation, and that is the cost.

    Quoting 0% violations without it would be the same selective reading the
    reports were built to prevent.
    """
    measured = _measured()
    pressure = [series["threefold"] for label, series in measured.items() if label.startswith("pressure")]
    assert len(pressure) == 2
    finished = sum(c["completions"] for c in pressure)
    runs = sum(c["n"] for c in pressure)
    gave_up = sum(c["gave_up"] for c in pressure)
    assert finished + gave_up == runs, (finished, gave_up, runs)
    for name in DOCUMENTS:
        body = _text(name)
        assert f"{finished} of {runs}" in body, f"{name} does not say the governed agent finished {finished} of {runs}"
