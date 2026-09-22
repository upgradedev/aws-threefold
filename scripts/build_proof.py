"""Builds src/threefold/web/proof.json, the snapshot the dashboard's #/proof page shows.

    python scripts/build_proof.py --benchmark benchmark/results/<run-id>.jsonl [--benchmark <more>]
    python scripts/build_proof.py --benchmark <rows or summary> \\
        --private-endpoint https://<host>/<stage>/ --key-file <file holding the operator key> \\
        [--evidence-base https://<where the repository's files can be read>/]

Two sections, each optional, each naming where it came from and when its
snapshot was taken. A section that was not measured is left out, and the page
says "not measured yet" rather than showing a zero.

benchmark   What benchmark/report.py computes from the result rows given, or
            from a summary its aggregate() produced, per condition, with its
            headline sentence exactly as report.headline() words it. Nothing
            here recomputes a rate: the numbers are report.py's own, so the
            page and docs/evidence/BENCHMARK_*.md cannot disagree.

private     The owner's own use, from GET api/overview?days=30 and GET
            api/projects on a private stack, read with the operator key. Only
            aggregate numbers are kept, each checked to be a number; no text
            read from the stack is copied at all, and an answer missing a
            field the contract fixes is refused rather than defaulted. The
            false-alarm rate is given only for a window in which nothing was
            refused: the overview counts false alarms on refused and observed
            calls alike but reviewed calls on observed ones only, so with a
            refusal in the window the two would not be the same calls.

The key is read from a file and sent only in the X-API-Key header, over HTTPS
or to this machine, and only to the address given: a redirect is refused, not
followed, because urllib would copy the header to wherever it pointed. It is
never printed, written or put in a URL, and neither is the endpoint, a project
name, a path or a developer. Before anything is written, every string in the
output is checked against every project name the private stack returned,
against the key, and against the shapes of an absolute path; if any matches,
nothing is written.

Sections are not carried over from an earlier snapshot: give every source each
time, so a section never outlives the data it was built from unnoticed.

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark import report  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "src" / "threefold" / "web" / "proof.json"
SCHEMA = 1
# The three conditions the headline compares. Each is listed even when no run
# measured it, so the page shows the gap rather than a table that quietly
# lost a row.
HEADLINE_CONDITIONS = ("none", "prompt", "threefold")
PRIVATE_WINDOW_DAYS = 30
TIMEOUT_SECONDS = 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# The bucket a name outside the stack's pattern is shown under. It names no
# project, so it may appear in the output.
UNLABELLED = "unlabelled"
SUMMARY_KEYS = ("pilot", "by_condition", "real_rows", "valid_rows", "invalid_reasons", "models", "tasks")

# The start of an absolute path on Windows, a UNC share, a home folder, or one
# of the usual roots of a POSIX machine. Repository-relative paths, which the
# evidence list carries on purpose, match none of these.
ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'(=])(?:[A-Za-z]:[\\/]|\\\\[^\\\s]|~[\\/]|/(?:Users|home|root|mnt|tmp|var|private|opt|srv|etc)/)"
)
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$")

Opener = Callable[..., Any]


class ProofError(RuntimeError):
    """A step failed. The message never names the key, the endpoint, a project, a path or a developer."""


# ---------------------------------------------------------------- small readers


def _relative(path: Path) -> str:
    """A source as the page may name it: its path in the repository, or its file name alone."""
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path).name


def _count(value: Any) -> Optional[int]:
    """A count read from the stack, or None when it is not one: nothing else is copied."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def _rounded(value: Optional[float], places: int = 4) -> Optional[float]:
    return None if value is None else round(value, places)


def _ratio(value: Optional[float], base: Optional[float]) -> Optional[float]:
    """report.py's overhead: the ratio of two means, or None when either is missing."""
    if value is None or not base:
        return None
    return round(value / base, 2)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- the benchmark


def read_benchmark(paths: Sequence[Path]) -> Tuple[Mapping[str, Any], List[str]]:
    """The summary report.aggregate() gives for these files, and the files as the page names them.

    A file holding one JSON object with `by_condition` is a summary aggregate()
    already produced; anything else is result rows, one JSON object a line. A
    summary cannot be merged with rows or with another summary without
    recomputing it differently from report.py, so that is refused.
    """
    rows: List[Dict[str, Any]] = []
    summaries: List[Mapping[str, Any]] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as error:
            raise ProofError(f"{Path(path).name} could not be read ({error.strerror or 'unreadable'})") from None
        except UnicodeDecodeError:
            raise ProofError(f"{Path(path).name} is not UTF-8 text, so it is not a file benchmark/run.py wrote") from None
        parsed: Any = None
        if text.lstrip().startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict) and "by_condition" in parsed:
            missing = [key for key in SUMMARY_KEYS if key not in parsed]
            if missing:
                raise ProofError(f"{Path(path).name} is not a summary benchmark/report.py produced: it lacks {', '.join(missing)}")
            summaries.append(parsed)
            continue
        try:
            loaded = report.load_rows([path])
        except ValueError as error:
            raise ProofError(str(error).replace(str(path), Path(path).name)) from None
        # A line that is JSON but not an object (an array, a number) would
        # reach report.py as a row it cannot read, and fail there with a trace.
        for number, row in enumerate(loaded, start=1):
            if not isinstance(row, dict):
                raise ProofError(f"{Path(path).name}: result row {number} is not a JSON object")
        rows.extend(loaded)
    if summaries and (rows or len(summaries) > 1):
        raise ProofError("a summary from benchmark/report.py cannot be combined with other results; give one summary, or the result rows")
    try:
        summary = summaries[0] if summaries else report.aggregate(rows)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ProofError(f"benchmark/report.py could not aggregate these rows ({_why(error)})") from None
    return summary, [_relative(Path(path)) for path in paths]


def _why(error: Exception) -> str:
    """What went wrong, by kind, without repeating anything that could be a path or a value from the input.

    A missing field names the field report.py wanted, which is its own name,
    not the input's; anything else is named by its type alone.
    """
    if isinstance(error, KeyError) and error.args and isinstance(error.args[0], str) and re.fullmatch(r"[a-z_]{1,40}", error.args[0]):
        return f"a row or condition lacks the field {error.args[0]!r}"
    return f"{type(error).__name__}, a value is not the shape report.py expects"


def _rate(stat: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "k": stat["k"],
        "n": stat["n"],
        "rate": _rounded(stat["rate"]),
        "ci_low": _rounded(stat["ci_low"]),
        "ci_high": _rounded(stat["ci_high"]),
    }


def _condition(name: str, stat: Mapping[str, Any], base: Optional[Mapping[str, Any]], unmeasured: int) -> Dict[str, Any]:
    refused = stat["refused_runs"]
    return {
        "condition": name,
        "label": report.CONDITION_LABELS.get(name, name),
        "n": stat["n"],
        "not_measured": unmeasured,
        "violation": _rate(stat["violation"]),
        "completion": _rate(stat["completion"]),
        # report.py's definition, per run: refused at least once, and still
        # finished with the acceptance tests passing and no violation.
        "self_correction": {
            "refused_runs": refused,
            "self_corrected": stat["self_corrected"],
            "rate": round(stat["self_corrected"] / refused, 4) if refused else None,
        },
        # Ratios of means against no guidance, as report.py prints them. The
        # baseline itself has none.
        "overhead": None if name == "none" else {
            "against": "none",
            "turns": _ratio(stat["turns_mean"], base["turns_mean"] if base else None),
            "seconds": _ratio(stat["seconds_mean"], base["seconds_mean"] if base else None),
            "cost": _ratio(stat["cost_mean"], base["cost_mean"] if base else None),
        },
    }


def benchmark_section(summary: Mapping[str, Any], sources: Sequence[str]) -> Dict[str, Any]:
    stats = summary["by_condition"]
    names = list(HEADLINE_CONDITIONS) + [name for name in summary.get("conditions") or [] if name not in HEADLINE_CONDITIONS]
    unmeasured = Counter(str(item.get("condition")) for item in summary.get("invalid") or [] if isinstance(item, Mapping))
    base = stats.get("none") if stats.get("none", {}).get("n") else None
    conditions = [
        _condition(name, stats.get(name) or report.condition_stats([]), base, unmeasured.get(name, 0))
        for name in names
    ]
    evidence = []
    written = report.default_output(summary)
    if written.is_file():
        evidence.append({"label": "The report benchmark/report.py wrote: method, every result and its limits",
                         "path": _relative(written)})
    evidence += [{"label": "The result rows it was computed from", "path": source} for source in sources]
    dates = list(summary.get("dates") or [])
    return {
        "source": "benchmark/report.py over " + ", ".join(sources),
        "snapshot_at": dates[-1] if dates else None,
        "pilot": bool(summary["pilot"]),
        "headline": report.headline(summary),
        "agent": "Claude Code",
        "agent_versions": list(summary.get("claude_versions") or []),
        "models": list(summary.get("models") or []),
        "dates": dates,
        "run_ids": list(summary.get("run_ids") or []),
        "tasks": list(summary.get("tasks") or []),
        "rows": summary.get("rows"),
        "real_rows": summary["real_rows"],
        "valid_rows": summary["valid_rows"],
        "scripted_rows": summary.get("scripted_rows", 0),
        "conditions": conditions,
        "evidence": evidence,
    }


# ---------------------------------------------------------------- the owner's own use


def read_key(path: Path) -> str:
    try:
        # utf-8-sig, because a key saved by Windows Notepad starts with a byte
        # order mark that strip() keeps and an HTTP header cannot carry.
        key = Path(path).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        raise ProofError("the key file could not be read as text") from None
    if not key or any(character.isspace() for character in key):
        raise ProofError("the key file must hold the operator key alone, on one line")
    return key


def endpoint_base(url: str) -> str:
    """The stack's base URL with a trailing slash, or a refusal to send a key there."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProofError("--private-endpoint must be the stack's https:// address")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ProofError("--private-endpoint must be a plain address, with no credentials, query or fragment in it")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise ProofError("the operator key is sent only over HTTPS, or to this machine")
    return url if url.endswith("/") else url + "/"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Follows no redirect, so the key goes only to the address the owner gave.

    urllib's own handler copies every header of a request, X-API-Key with the
    rest, into the request it redirects to, whatever host and scheme the
    answer names: a stack, or anything posing as one, could send the key on
    to any address, over plain http. Declining here makes urllib raise the
    3xx as an HTTPError, which fetch_json reports without the address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_opener(base: str) -> Opener:
    """The opener the key is sent with: no redirect followed, and no proxy for a plain http address.

    A proxy the environment names would read an http request's headers, and
    plain http is allowed only because the address is this machine; over
    https the proxy sees only a tunnel, so the environment's is kept.
    """
    handlers: List[Any] = [_RefuseRedirects()]
    if urllib.parse.urlsplit(base).scheme == "http":
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open


def fetch_json(base: str, route: str, key: str, opener: Optional[Opener] = None) -> Mapping[str, Any]:
    """GET one route with the key in its header. Errors name the route, never the address or the key."""
    request = urllib.request.Request(base + route, headers={"X-API-Key": key, "Accept": "application/json"})
    shown = "GET /" + route.split("?", 1)[0]
    try:
        with (opener or default_opener(base))(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise ProofError(f"the private stack answered {shown} with a redirect (HTTP {error.code}), which is not "
                             "followed: the key is sent to the address given and nowhere else") from None
        raise ProofError(f"the private stack answered HTTP {error.code} to {shown}") from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        raise ProofError(f"the private stack could not be read at {shown}") from None
    if not isinstance(body, dict):
        raise ProofError(f"the private stack's answer to {shown} is not a JSON object")
    return body


def read_private(url: str, key: str, opener: Optional[Opener] = None) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    base = endpoint_base(url)
    overview = fetch_json(base, f"api/overview?days={PRIVATE_WINDOW_DAYS}", key, opener)
    projects = fetch_json(base, "api/projects", key, opener)
    return overview, projects


def project_names(overview: Mapping[str, Any], projects: Mapping[str, Any]) -> Set[str]:
    """Every project name the two answers carry: what the output must never contain."""
    names: Set[str] = set()
    for listing in (overview.get("by_project"), projects.get("projects")):
        for row in listing if isinstance(listing, list) else []:
            if isinstance(row, Mapping) and isinstance(row.get("project"), str):
                names.add(row["project"])
    names.discard("")
    names.discard(UNLABELLED)
    return names


# What GET /api/overview must carry for a private section to be built. The
# contract fixes every one of these; an answer missing one is not a stack this
# script understands, and a section built from it would show a zero or a gap
# nobody measured.
OVERVIEW_TOTALS = ("calls", "approved", "refused", "would_refuse", "needs_review", "false_alarms", "projects", "agents")
SERIES_COUNTS = ("approved", "observed", "refused")
STAGES = ("observe", "enforce")


def _not_the_contract(what: str) -> ProofError:
    """A refusal that names the field, which is the contract's own word, and never the value the stack sent."""
    return ProofError(f"the private stack's answer to GET /api/overview is not the shape the contract fixes: {what}")


def _self_correction_totals(figure: Any) -> Optional[Dict[str, Any]]:
    """The self-correction figure's numbers, or None for a stack that predates it."""
    if figure is None:
        return None
    if not isinstance(figure, Mapping):
        raise _not_the_contract("self_correction is not an object")
    # rows_read is kept so the page can say how much a read that stopped short
    # covered; it is a count of calls and names nothing.
    counts = {name: _count(figure.get(name)) for name in ("refusals_considered", "self_corrected", "rows_read")}
    if None in counts.values() or not isinstance(figure.get("complete"), bool):
        raise _not_the_contract("self_correction does not carry its counts and complete")
    numbers = {}
    for name in ("rate", "median_calls_to_correct"):
        value = figure.get(name)
        if value is not None and _number(value) is None:
            raise _not_the_contract(f"self_correction.{name} is neither a number nor null")
        numbers[name] = value
    return dict(counts, **numbers, complete=figure["complete"])


def private_section(overview: Mapping[str, Any]) -> Dict[str, Any]:
    """The totals of the owner's own use: numbers only, each checked to be one, or a refusal.

    Nothing is defaulted. A total, a day of the series or a stage count that
    is missing or is not a count stops the build, so the page never shows a
    figure the stack did not give; and no text the stack sent is copied.
    """
    totals = overview.get("totals")
    if not isinstance(totals, Mapping):
        raise _not_the_contract("it has no totals")
    counts: Dict[str, int] = {}
    for name in OVERVIEW_TOTALS:
        value = _count(totals.get(name))
        if value is None:
            raise _not_the_contract(f"totals.{name} is not a count")
        counts[name] = value
    series = overview.get("series")
    if not isinstance(series, list) or not series:
        raise _not_the_contract("it has no daily series")
    if not all(isinstance(day, Mapping) and all(_count(day.get(kind)) is not None for kind in SERIES_COUNTS) for day in series):
        raise _not_the_contract("a day of its series is not a count of approved, observed and refused calls")
    stages = overview.get("stages")
    if not isinstance(stages, Mapping) or any(_count(stages.get(stage)) is None for stage in STAGES):
        raise _not_the_contract("stages is not a count per stage")
    window = _count(overview.get("window_days"))
    if not window:
        raise _not_the_contract("window_days is not a number of days")
    generated = overview.get("generated_at")
    if not (isinstance(generated, str) and TIMESTAMP.match(generated)):
        raise _not_the_contract("generated_at is not a timestamp")

    # needs_review counts the would-refuse calls nobody has labelled, so the
    # labelled ones are the difference: labels on observed calls only.
    # false_alarms counts every false-alarm label, on a refused call as well
    # as an observed one, and the overview gives no count of labelled refused
    # calls to add to the reviewed side. The two are the same calls only when
    # nothing was refused in the window, as on a stack that only observes.
    # Otherwise the rate would divide unlike counts, and could pass 100%, so
    # both are left out rather than guessed.
    reviewed = max(0, counts["would_refuse"] - counts["needs_review"])
    comparable = counts["refused"] == 0 and counts["false_alarms"] <= reviewed
    return {
        "source": f"GET /api/overview?days={PRIVATE_WINDOW_DAYS} and GET /api/projects on the owner's private stack, "
                  "read with the operator key; only these totals were kept",
        "snapshot_at": generated,
        "window_days": window,
        "days_observed": sum(1 for day in series if sum(day[kind] for kind in SERIES_COUNTS) > 0),
        "calls_governed": counts["calls"],
        "would_refuse": counts["would_refuse"],
        "refused": counts["refused"],
        "reviewed": reviewed,
        "false_alarms": counts["false_alarms"] if comparable else None,
        "false_alarm_rate": round(counts["false_alarms"] / reviewed, 4) if comparable and reviewed else None,
        "projects": counts["projects"],
        "agents": counts["agents"],
        "stages": {stage: stages[stage] for stage in STAGES},
        "self_correction": _self_correction_totals(overview.get("self_correction")),
    }


# ---------------------------------------------------------------- how it was measured


METHOD = (
    ("The benchmark harness, its tasks and its independent checkers", "benchmark/README.md"),
    ("How the benchmark's rows become rates and a headline", "benchmark/report.py"),
    ("How self-correction is computed from the ledger", "src/threefold/application/insights.py"),
    ("The routes the owner's totals are read from", "src/threefold/web/openapi.json"),
    ("How this snapshot was built, and what it refuses to write", "scripts/build_proof.py"),
)


def method_entries() -> List[Dict[str, str]]:
    """The evidence files, by their path in the repository, only those that exist."""
    return [{"label": label, "path": path} for label, path in METHOD if (REPO_ROOT / path).is_file()]


def evidence_base(url: Optional[str]) -> Optional[str]:
    """Where the repository's files can be read, for the evidence links, or None to list paths only.

    Given by the owner, never guessed: a link to a repository that is not
    there is worse than a path a reader can look up.
    """
    if url is None:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ProofError("--evidence-base must be a plain https:// address")
    return url.rstrip("/") + "/"


def _linked(items: Iterable[Mapping[str, Any]], base: Optional[str]) -> List[Dict[str, Any]]:
    if base is None:
        return [dict(item) for item in items]
    return [dict(item, href=base + urllib.parse.quote(str(item["path"]))) for item in items]


# ---------------------------------------------------------------- the check before writing


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def leaks(document: Any, names: Iterable[str], secrets: Iterable[str] = ()) -> List[str]:
    """What the document would give away, by kind only: the offending value is never repeated."""
    lowered = [name.lower() for name in names if name]
    kept = [secret for secret in secrets if secret]
    found: Set[str] = set()
    for text in _strings(document):
        folded = text.lower()
        if any(name in folded for name in lowered):
            found.add("a project name read from the private stack")
        if any(secret in text for secret in kept):
            found.add("the operator key")
        if ABSOLUTE_PATH.search(text):
            found.add("an absolute path")
    return sorted(found)


def series_label(path: Path, summary: Mapping[str, Any]) -> Tuple[str, str]:
    """The family and the words a series is shown under: its task family and its model.

    A series is one run of the matrix. Two models, or the standard and the
    pressure tasks, are never pooled into one set of rates: each is its own
    section, and this names it so a reader can tell them apart.
    """
    family = "standard"
    if str(path).endswith(".jsonl"):
        try:
            families = {str(row.get("family") or "standard") for row in report.load_rows([path]) if isinstance(row, dict)}
        except ValueError:
            families = set()
        if families == {"pressure"}:
            family = "pressure"
    models = ", ".join(summary.get("models") or []) or "model not recorded"
    words = "Pressure tasks, where the prompt asks for the shortcut" if family == "pressure" else "Standard tasks"
    return family, f"{words} · {models}"


def build(
    benchmarks: Sequence[Path],
    private_endpoint: Optional[str] = None,
    key_file: Optional[Path] = None,
    opener: Optional[Opener] = None,
    evidence_url: Optional[str] = None,
    series: Sequence[Path] = (),
) -> Dict[str, Any]:
    document: Dict[str, Any] = {"schema": SCHEMA, "generated_at": _now(), "generated_by": "scripts/build_proof.py"}
    names: Set[str] = set()
    secrets: List[str] = []
    base = evidence_base(evidence_url)
    if benchmarks:
        summary, sources = read_benchmark(benchmarks)
        try:
            document["benchmark"] = benchmark_section(summary, sources)
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ProofError(f"the benchmark summary is not one benchmark/report.py produced ({_why(error)})") from None
        document["benchmark"]["evidence"] = _linked(document["benchmark"]["evidence"], base)
    if series:
        sections = []
        for path in series:
            summary, sources = read_benchmark([path])
            try:
                section = benchmark_section(summary, sources)
            except (KeyError, TypeError, AttributeError, ValueError) as error:
                raise ProofError(f"the benchmark summary is not one benchmark/report.py produced ({_why(error)})") from None
            section["family"], section["label"] = series_label(Path(path), summary)
            section["evidence"] = _linked(section["evidence"], base)
            sections.append(section)
        document["benchmarks"] = sections
        # The page's single card and older readers look here; the first series
        # given is the one they show.
        document.setdefault("benchmark", sections[0])
    if private_endpoint is not None:
        key = read_key(key_file)
        secrets.append(key)
        overview, projects = read_private(private_endpoint, key, opener)
        names = project_names(overview, projects)
        document["private"] = private_section(overview)
    document["method"] = _linked(method_entries(), base)
    problems = leaks(document, names, secrets)
    if problems:
        raise ProofError("refused to write: the snapshot would carry " + " and ".join(problems))
    return document


def main(argv: Optional[Sequence[str]] = None, opener: Optional[Opener] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the proof page's snapshot.")
    parser.add_argument("--benchmark", action="append", type=Path, default=[],
                        help="benchmark result rows (.jsonl) or a summary from benchmark/report.py; repeatable")
    parser.add_argument("--series", action="append", type=Path, default=[],
                        help="one run of the matrix (.jsonl), shown as its own section and never pooled with another; repeatable")
    parser.add_argument("--private-endpoint", default=None, help="the private stack's https:// address")
    parser.add_argument("--key-file", type=Path, default=None, help="a file holding the operator key, alone on one line")
    parser.add_argument("--evidence-base", default=None,
                        help="an https:// address the repository's files are read under; without it the evidence is listed by path")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="default: src/threefold/web/proof.json")
    args = parser.parse_args(argv)
    if (args.private_endpoint is None) != (args.key_file is None):
        parser.error("--private-endpoint and --key-file go together")
    if not args.benchmark and not args.series and args.private_endpoint is None:
        parser.error("nothing to build: give --benchmark or --series, or --private-endpoint with --key-file")
    try:
        document = build(args.benchmark, args.private_endpoint, args.key_file, opener, args.evidence_base, args.series)
    except ProofError as error:
        print(f"build_proof: {error}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    bench = document.get("benchmark")
    said = [
        (f"benchmark{' (PILOT)' if bench['pilot'] else ''}: {bench['valid_rows']} of {bench['real_rows']} real-agent run(s) measured"
         if bench else "no benchmark section"),
        "a private section of totals" if "private" in document else "no private section",
    ]
    if document.get("benchmarks"):
        said.append(f"{len(document['benchmarks'])} series shown apart")
    print(f"{args.out.name} written: " + "; ".join(said))
    return 0


if __name__ == "__main__":
    sys.exit(main())
