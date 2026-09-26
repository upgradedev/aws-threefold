"""Figures and promises a document makes about files in this repository.

Two ways a document goes quietly wrong. It writes down a number taken from a
file (the runbook's template sizes, which the template outgrew, and what its
benchmark runs took, which is hand-computed from their rows), or it promises a
file that is not there (the enforcement evidence's rerunnable script). Both are
checked here against the repository itself, so the document fails rather than a
reader finding out.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# What CloudFormation accepts for a template sent inline, in bytes.
INLINE_TEMPLATE_LIMIT = 51200
# What it accepts for a template read from an S3 object, in bytes: the limit
# the runbook's commands work under, since they send the template through the
# packaging bucket.
S3_TEMPLATE_LIMIT = 1_000_000

# A size written with digits, in any unit and however the digits are grouped.
SIZE = r"\d[\d,]*(?:\.\d+)?\s*(?:bytes?|KiB|MiB|[kKMG]B|kilobytes?|megabytes?)\b"

# A script a document names. Hyphens and dots are in the class because a name
# that carries one is still a promise: `scripts/pre-commit-gate` is committed
# under exactly that spelling.
SCRIPT = r"scripts/[A-Za-z0-9_.-]+\.py"

# The four measured matrices, as rows, in the order the runbook's benchmark
# table lists them beside their reports.
BENCHMARK_RUNS = (
    "20260922T143932Z.jsonl",
    "20260922T145644Z.jsonl",
    "20260922T161455Z-pressure.jsonl",
    "20260922T162306Z-pressure.jsonl",
)

# How the runbook states what a run took. The bounds above it, which come from
# the harness's caps rather than from any run, are written without "about", so
# they are not measured figures and are not checked against the rows.
RUN_FIGURES = r"about \d+(?:\.\d+)? hours? and \$\d{1,3}(?:,\d{3})*"


def _text(name: str) -> str:
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


def _size(name: str) -> int:
    """Bytes on disk. `.gitattributes` forces LF, so this is the committed size."""
    return len((ROOT / name).read_bytes())


def _ran(rows_file: str) -> tuple[float, float]:
    """What one matrix took: (hours of wall clock, dollars), from its own rows.

    A row carries `started_at` and `total_seconds` and no finish time, so a run
    ends at the one plus the other. The matrix runs several at a time, so the
    hours are the span from the first start to the last finish; adding the runs
    up would report several times what the matrix occupied. `total_seconds` is
    the harness's own measure of a run, which is the longer of the two it
    records, so the span is the generous reading of both.
    """
    lines = (ROOT / "benchmark" / "results" / rows_file).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    started = [datetime.datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")) for row in rows]
    ended = [at + datetime.timedelta(seconds=float(row["total_seconds"])) for at, row in zip(started, rows)]
    hours = (max(ended) - min(started)).total_seconds() / 3600
    return hours, sum(float(row["cost_usd"]) for row in rows)


def test_the_runbook_states_what_the_benchmark_matrices_took_as_their_rows_have_it() -> None:
    """The hours and dollars beside each matrix, read back from the rows.

    They replaced the section's stale estimate, and they are the same kind of
    number as the template sizes above: computed by hand from a committed file,
    and wrong the moment a run is added, rebuilt or dropped. Nothing else
    derives them, because the summaries `report.py` writes carry no total.
    """
    stated = set(re.findall(RUN_FIGURES, _text("docs/RUNBOOK.md")))
    measured = set()
    for rows_file in BENCHMARK_RUNS:
        hours, cost = _ran(rows_file)
        measured.add(f"about {hours:.1f} hours and ${cost:,.0f}")
    assert len(measured) == len(BENCHMARK_RUNS), f"Two matrices are stated alike, so this proves less: {sorted(measured)}"
    assert stated == measured, f"The runbook says {sorted(stated)}; the rows give {sorted(measured)}"


def test_the_runbook_states_the_inline_limit_and_no_size_that_goes_stale() -> None:
    runbook = _text("docs/RUNBOOK.md")
    assert "51,200 bytes" in runbook, "The runbook's deploy section must name the limit it works around"

    # The only size in the runbook is the limit itself. A size read off a file
    # is wrong the next time that file is edited, which is how this broke. It is
    # matched however it is spelled — grouped or not, in bytes, kilobytes or
    # kibibytes — so the guard cannot be walked around by writing "48 KiB".
    counts = set(re.findall(SIZE, runbook))
    assert counts == {"51,200 bytes"}, f"The runbook records sizes that go stale: {sorted(counts - {'51,200 bytes'})}"

    template = _size("deploy/template.yml")
    edge = _size("deploy/edge.yml")
    assert template < S3_TEMPLATE_LIMIT, "The template no longer fits even through the packaging bucket"
    assert "has grown past the 51,200 bytes" in runbook, "The runbook must say why the template goes through the bucket"
    assert template > INLINE_TEMPLATE_LIMIT, (
        f"The runbook says the template has grown past the inline limit; it has {INLINE_TEMPLATE_LIMIT - template} "
        "bytes to spare"
    )
    assert INLINE_TEMPLATE_LIMIT - edge > 2048, (
        f"The runbook calls edge.yml comfortably under the limit; it has {INLINE_TEMPLATE_LIMIT - edge} bytes left"
    )


def test_the_enforcement_evidence_promises_no_script_that_is_not_committed() -> None:
    evidence = _text("docs/evidence/ENFORCEMENT_2026-09-21.md")
    assert "the check is a script that can be rerun" not in evidence
    assert "No script in this repository runs it" in evidence

    named = set(re.findall(SCRIPT, evidence))
    missing = {name for name in named if not (ROOT / name).exists()}
    # A script the evidence names as existing would have to be committed; the
    # one it names as missing is named in the sentence that says it is missing.
    for name in missing:
        assert f"a committed `{name}` would make this table" in evidence, f"{name} is promised but not committed"


def test_the_codex_evidence_names_a_committed_harness_and_does_not_claim_it_made_the_runs() -> None:
    """The 2026-09-21 file promised a script that was not there; this one ships the script.

    What it must not do is let the command it prints stand for the runs it
    reports. Those were driven one condition at a time while the script was
    still being corrected, so the file says the command cannot reproduce them.
    """
    evidence = _text("docs/evidence/ENFORCEMENT_2026-09-23.md")
    named = set(re.findall(SCRIPT, evidence))
    assert "scripts/measure_codex_enforcement.py" in named, "The evidence no longer names the harness it ships"
    for name in sorted(named):
        assert (ROOT / name).exists(), f"{name} is named by the evidence and not committed"
    assert "cannot reproduce them as they happened" in evidence
