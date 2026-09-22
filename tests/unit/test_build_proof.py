"""scripts/build_proof.py: the proof page's snapshot, and what it refuses to carry.

The benchmark section must be report.py's own numbers and headline, never a
second computation of them. The private section is read from a stack with the
operator key, so the tests stand one up on this machine (a stdlib HTTP server
on 127.0.0.1, answering as the contract shapes /api/overview and
/api/projects) and check that no project name it returns, and never the key,
reaches the output file, the terminal or an error message.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "benchmark" / "results" / "20260922T095056Z-pilot.jsonl"
COMMITTED = ROOT / "src" / "threefold" / "web" / "proof.json"
KEY = "acme-operator-key-7f3c9a2e5b1d"
SECRET_PROJECTS = ("Acme-Proj-Lighthouse", "Acme-Proj-Harbour")

_spec = importlib.util.spec_from_file_location("build_proof", ROOT / "scripts" / "build_proof.py")
build_proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_proof)
report = build_proof.report


def _run(condition: str, rep: int, **fields) -> dict:
    row = {
        "agent": "claude-code", "condition": condition, "task": "orders-s3-archive", "rep": rep,
        "run_id": "20260930T090000Z", "started_at": "2026-09-30T09:00:00Z", "model": "claude-sonnet-5",
        "claude_code_version": "2.1.220", "pilot": False, "measured": True, "agent_ran": True,
        "harness_error": None, "acceptance_passed": True, "violation_landed": False,
        "refused_at_least_once": False, "self_corrected": None, "num_turns": 10, "duration_ms": 60000,
        "cost_usd": 0.5, "harness": {"platform": "Windows"}, "isolation": {"mode": "fresh-config"},
    }
    row.update(fields)
    return row


def _rows_file(tmp_path: Path, rows, name="rows.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


MEASURED = [
    _run("none", 1, violation_landed=True), _run("none", 2),
    _run("prompt", 1, violation_landed=True), _run("prompt", 2, acceptance_passed=False),
    _run("threefold", 1, refused_at_least_once=True, self_corrected=True, num_turns=15, duration_ms=90000, cost_usd=0.75),
    _run("threefold", 2, refused_at_least_once=True, self_corrected=False, acceptance_passed=False, num_turns=15,
         duration_ms=90000, cost_usd=0.75),
    _run("threefold", 3, agent_ran=False, measured=False, agent_error="usage limit reached"),
]


# ---------------------------------------------------------------- the benchmark


def test_the_pilot_snapshot_is_report_pys_own_headline_and_marks_itself_a_pilot() -> None:
    summary, sources = build_proof.read_benchmark([PILOT])
    section = build_proof.benchmark_section(summary, sources)
    assert section["pilot"] is True
    assert section["headline"] == report.headline(report.aggregate(report.load_rows([PILOT])))
    assert section["headline"].startswith("PILOT, not a result")
    assert [c["condition"] for c in section["conditions"]] == ["none", "prompt", "threefold"]
    for condition in section["conditions"]:
        assert condition["n"] == 0 and condition["not_measured"] == 1
        assert condition["violation"]["rate"] is None, "A condition nothing measured has no rate, not a zero"
    assert section["source"] == "benchmark/report.py over benchmark/results/20260922T095056Z-pilot.jsonl"
    assert section["snapshot_at"] == "2026-09-22" and section["models"] == ["claude-sonnet-5"]
    for item in section["evidence"]:
        assert (ROOT / item["path"]).is_file(), f"{item['path']} is named as evidence and does not exist"


def test_each_conditions_figures_are_the_ones_report_py_computes(tmp_path: Path) -> None:
    path = _rows_file(tmp_path, MEASURED)
    section = build_proof.benchmark_section(*build_proof.read_benchmark([path]))
    stats = report.aggregate(MEASURED)["by_condition"]
    by_name = {c["condition"]: c for c in section["conditions"]}
    for name in ("none", "prompt", "threefold"):
        assert by_name[name]["n"] == stats[name]["n"]
        assert by_name[name]["violation"]["rate"] == round(stats[name]["violation"]["rate"], 4)
        assert by_name[name]["completion"]["k"] == stats[name]["completion"]["k"]
        assert by_name[name]["violation"]["ci_low"] == round(stats[name]["violation"]["ci_low"], 4)
    threefold = by_name["threefold"]
    assert threefold["n"] == 2 and threefold["not_measured"] == 1, "A run that measured nothing is counted apart, never as clean"
    assert threefold["self_correction"] == {"refused_runs": 2, "self_corrected": 1, "rate": 0.5}
    assert threefold["overhead"] == {"against": "none", "turns": 1.5, "seconds": 1.5, "cost": 1.5}
    assert by_name["none"]["overhead"] is None and by_name["none"]["self_correction"]["rate"] is None
    assert section["pilot"] is False and section["headline"] == report.headline(report.aggregate(MEASURED))
    assert section["source"] == "benchmark/report.py over rows.jsonl", "A file outside the repository is named by its file name alone"


def test_a_summary_report_py_produced_gives_the_same_section_as_its_rows(tmp_path: Path) -> None:
    rows_path = _rows_file(tmp_path, MEASURED)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(report.aggregate(MEASURED)), encoding="utf-8")
    from_rows = build_proof.benchmark_section(*build_proof.read_benchmark([rows_path]))
    from_summary = build_proof.benchmark_section(*build_proof.read_benchmark([summary_path]))
    for key in ("headline", "conditions", "pilot", "valid_rows", "real_rows", "models"):
        assert from_summary[key] == from_rows[key]


def test_several_row_files_are_one_set_of_rows(tmp_path: Path) -> None:
    first = _rows_file(tmp_path, MEASURED[:3], "a.jsonl")
    second = _rows_file(tmp_path, MEASURED[3:], "b.jsonl")
    joined = build_proof.benchmark_section(*build_proof.read_benchmark([first, second]))
    assert joined["headline"] == report.headline(report.aggregate(MEASURED))


def test_a_summary_is_never_merged_with_anything_else(tmp_path: Path, capsys) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(report.aggregate(MEASURED)), encoding="utf-8")
    rows_path = _rows_file(tmp_path, MEASURED)
    out = tmp_path / "proof.json"
    assert build_proof.main(["--benchmark", str(summary_path), "--benchmark", str(rows_path), "--out", str(out)]) == 2
    assert "cannot be combined" in capsys.readouterr().err and not out.exists()


def test_a_json_object_that_is_not_a_summary_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "odd.json"
    path.write_text(json.dumps({"by_condition": {}}), encoding="utf-8")
    with pytest.raises(build_proof.ProofError, match="not a summary"):
        build_proof.read_benchmark([path])


@pytest.mark.parametrize("name, content", [
    ("no-task.jsonl", (json.dumps({k: v for k, v in _run("none", 1).items() if k != "task"}) + "\n").encode("utf-8")),
    ("not-utf8.jsonl", b"\xff\xfe\x00garbage\n"),
    ("array.jsonl", b"[1, 2, 3]\n"),
    ("number.jsonl", b"42\n"),
    ("summary.json", json.dumps(dict(report.aggregate(MEASURED), by_condition={"none": {"n": 1}})).encode("utf-8")),
])
def test_malformed_benchmark_input_is_refused_without_a_trace_or_a_path(tmp_path, capsys, name, content) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    out = tmp_path / "proof.json"
    assert build_proof.main(["--benchmark", str(path), "--out", str(out)]) == 2
    printed = capsys.readouterr()
    assert printed.err.startswith("build_proof: ") and "Traceback" not in printed.err and not out.exists()
    assert str(tmp_path) not in printed.err and not build_proof.leaks(printed.err, ())


SERIES = [
    ROOT / "benchmark" / "results" / name
    for name in (
        "20260922T143932Z.jsonl",
        "20260922T145644Z.jsonl",
        "20260922T161455Z-pressure.jsonl",
        "20260922T162306Z-pressure.jsonl",
    )
]


def test_the_committed_snapshot_is_what_the_script_builds_from_the_committed_series() -> None:
    """The benchmark part of the snapshot can be rebuilt from the rows in the repository.

    The private part cannot: it was read from the owner's stack with the key,
    and only its totals were kept, so it is checked for shape, not rebuilt.
    """
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    rebuilt = build_proof.build([], series=SERIES)
    assert committed["benchmarks"] == rebuilt["benchmarks"], (
        "src/threefold/web/proof.json is stale: rebuild it with one --series per committed run")
    assert committed["benchmark"] == rebuilt["benchmarks"][0]
    private = committed.get("private")
    if private is not None:
        assert set(private) <= {
            "source", "snapshot_at", "window_days", "days_observed", "calls_governed", "would_refuse", "refused",
            "reviewed", "false_alarms", "false_alarm_rate", "projects", "agents", "stages", "self_correction",
        }


def test_each_series_is_its_own_section_and_never_pooled(tmp_path: Path) -> None:
    sonnet = _rows_file(tmp_path, MEASURED, "sonnet.jsonl")
    haiku_rows = [dict(row, model="claude-haiku-4-5", violation_landed=(row["condition"] == "none")) for row in MEASURED]
    haiku = _rows_file(tmp_path, haiku_rows, "haiku.jsonl")
    pressure = _rows_file(tmp_path, [dict(row, family="pressure", task="pressure-orders-boto3-entity") for row in MEASURED],
                          "pressure.jsonl")
    document = build_proof.build([], series=[sonnet, haiku, pressure])
    sections = document["benchmarks"]
    assert [section["label"] for section in sections] == [
        "Standard tasks · claude-sonnet-5",
        "Standard tasks · claude-haiku-4-5",
        "Pressure tasks, where the prompt asks for the shortcut · claude-sonnet-5",
    ]
    assert [section["family"] for section in sections] == ["standard", "standard", "pressure"]
    for section, path in zip(sections, (sonnet, haiku, pressure)):
        alone = build_proof.build([path])["benchmark"]
        assert section["conditions"] == alone["conditions"], "a series must equal the same rows built alone"
    assert document["benchmark"] == sections[0]


def test_the_command_line_takes_several_series(tmp_path: Path, capsys) -> None:
    first = _rows_file(tmp_path, MEASURED, "a.jsonl")
    second = _rows_file(tmp_path, MEASURED, "b.jsonl")
    out = tmp_path / "proof.json"
    assert build_proof.main(["--series", str(first), "--series", str(second), "--out", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))["benchmarks"]) == 2
    assert "2 series shown apart" in capsys.readouterr().out


def test_evidence_becomes_links_only_under_an_https_address_the_owner_gives(tmp_path: Path) -> None:
    listed = build_proof.build([PILOT])
    assert all("href" not in item for item in listed["benchmark"]["evidence"] + listed["method"])
    linked = build_proof.build([PILOT], evidence_url="https://example.test/acme/threefold/blob/main")
    for item in linked["benchmark"]["evidence"] + linked["method"]:
        assert item["href"] == "https://example.test/acme/threefold/blob/main/" + item["path"]
    for bad in ("http://example.test/acme", "https://example.test/acme?x=1", "file:///acme"):
        with pytest.raises(build_proof.ProofError, match="plain https"):
            build_proof.build([PILOT], evidence_url=bad)


def test_nothing_to_build_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        build_proof.main(["--out", str(tmp_path / "proof.json")])
    assert stopped.value.code == 2
    with pytest.raises(SystemExit):
        build_proof.main(["--benchmark", str(PILOT), "--private-endpoint", "https://acme.example/prod/"])


# ---------------------------------------------------------------- the owner's own use


def _overview(names):
    return {
        "window_days": 30, "generated_at": "2026-09-29T08:00:00+00:00", "source": "rollups",
        "totals": {"calls": 4210, "approved": 4000, "refused": 0, "would_refuse": 210, "needs_review": 60,
                   "false_alarms": 12, "projects": len(names), "agents": 2},
        "series": [{"day": "2026-09-27", "approved": 900, "observed": 40, "refused": 0},
                   {"day": "2026-09-28", "approved": 0, "observed": 0, "refused": 0},
                   {"day": "2026-09-29", "approved": 3100, "observed": 170, "refused": 0}],
        "by_agent": [{"agent": "claude-code", "calls": 3000}, {"agent": "antigravity", "calls": 1210}],
        "by_rule": [{"rule_key": "python-domain-stays-pure", "refused": 0, "would_refuse": 210}],
        "by_project": [{"project": name, "stage": "observe", "calls": 2105, "last_seen": "2026-09-29T08:00:00+00:00"}
                       for name in names],
        "stages": {"observe": len(names), "enforce": 0},
        "self_correction": {"refusals_considered": 0, "self_corrected": 0, "rate": None,
                            "median_calls_to_correct": None, "rows_read": 1900, "complete": True},
    }


class _Stack:
    """A private stack on this machine: it answers the two reads and records what it was sent."""

    def __init__(self, overview, projects, status=200, redirect=None):
        self.seen = []
        stack = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the stdlib's name
                stack.seen.append({"path": self.path, "key": self.headers.get("X-API-Key")})
                if redirect is not None and self.path.startswith("/prod/"):
                    # Sends the client on, as a hostile or misconfigured stack might.
                    self.send_response(redirect)
                    self.send_header("Location", "/stolen" + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = overview if self.path.startswith("/prod/api/overview") else projects
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/prod/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "operator.key"
    path.write_text(KEY + "\n", encoding="utf-8")
    return path


def _projects(names):
    return {"projects": [{"project": name, "stage": "observe", "configured": True, "calls": 2105,
                          "agents": ["claude-code"], "hook_modes": ["observe"]} for name in names]}


def test_the_private_section_keeps_totals_and_nothing_that_names_anything(tmp_path, key_file, capsys) -> None:
    out = tmp_path / "proof.json"
    with _Stack(_overview(SECRET_PROJECTS), _projects(SECRET_PROJECTS + ("Acme-Proj-Quiet",))) as stack:
        code = build_proof.main(["--benchmark", str(PILOT), "--private-endpoint", stack.url,
                                 "--key-file", str(key_file), "--out", str(out)])
    printed = capsys.readouterr()
    assert code == 0, printed.err
    written = out.read_text(encoding="utf-8")
    private = json.loads(written)["private"]
    assert (private["calls_governed"], private["would_refuse"], private["reviewed"], private["false_alarms"]) == (4210, 210, 150, 12)
    assert private["false_alarm_rate"] == 0.08 and private["days_observed"] == 2 and private["window_days"] == 30
    assert (private["projects"], private["agents"], private["stages"]) == (2, 2, {"observe": 2, "enforce": 0})
    assert private["self_correction"]["refusals_considered"] == 0 and private["snapshot_at"] == "2026-09-29T08:00:00+00:00"
    for name in SECRET_PROJECTS + ("Acme-Proj-Quiet", "Lighthouse", "claude-code", "python-domain-stays-pure"):
        assert name not in written, f"{name} reached the snapshot"
    # The key went in the header of each read, and nowhere else.
    assert [(hit["path"], hit["key"]) for hit in stack.seen] == [("/prod/api/overview?days=30", KEY), ("/prod/api/projects", KEY)]
    for text in (written, printed.out, printed.err):
        assert KEY not in text and "127.0.0.1" not in text


def test_a_project_name_that_would_reach_the_output_stops_the_write(tmp_path, key_file, capsys) -> None:
    out = tmp_path / "proof.json"
    # A stack whose project shares its name with a benchmark task: the task
    # list would carry it into the snapshot.
    named = ("orders-s3-archive",)
    with _Stack(_overview(named), _projects(named)) as stack:
        code = build_proof.main(["--benchmark", str(PILOT), "--private-endpoint", stack.url,
                                 "--key-file", str(key_file), "--out", str(out)])
    printed = capsys.readouterr()
    assert code == 2 and not out.exists()
    assert "refused to write" in printed.err and "a project name" in printed.err
    assert "orders-s3-archive" not in printed.err, "The refusal names the kind of leak, never the name"


def test_a_refusal_from_the_stack_names_the_route_and_never_the_key(tmp_path, key_file, capsys) -> None:
    out = tmp_path / "proof.json"
    with _Stack({"detail": "no"}, {"detail": "no"}, status=403) as stack:
        code = build_proof.main(["--private-endpoint", stack.url, "--key-file", str(key_file), "--out", str(out)])
    printed = capsys.readouterr()
    assert code == 2 and not out.exists()
    assert "HTTP 403 to GET /api/overview" in printed.err
    assert KEY not in printed.out + printed.err and "127.0.0.1" not in printed.err


@pytest.mark.parametrize("endpoint", [
    "http://acme-private.example/prod/",
    "https://user:pw@acme-private.example/prod/",
    "https://acme-private.example/prod/?key=1",
    "ftp://acme-private.example/",
])
def test_the_key_is_sent_only_over_https_or_to_this_machine(tmp_path, key_file, capsys, endpoint) -> None:
    opened = []
    code = build_proof.main(["--private-endpoint", endpoint, "--key-file", str(key_file),
                             "--out", str(tmp_path / "proof.json")], opener=lambda *a, **k: opened.append(a))
    assert code == 2 and not opened, "Refused before any request was made"
    assert KEY not in capsys.readouterr().err


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_refused_so_the_key_goes_nowhere_else(tmp_path, key_file, capsys, status) -> None:
    # urllib copies every header into the request it redirects to, whatever
    # host and scheme the answer names, so a followed redirect would carry
    # the key to an address the owner never gave.
    out = tmp_path / "proof.json"
    with _Stack(_overview(SECRET_PROJECTS), _projects(SECRET_PROJECTS), redirect=status) as stack:
        code = build_proof.main(["--private-endpoint", stack.url, "--key-file", str(key_file), "--out", str(out)])
    printed = capsys.readouterr()
    assert code == 2 and not out.exists()
    assert [hit["path"] for hit in stack.seen] == ["/prod/api/overview?days=30"], "The redirect was not followed"
    assert f"redirect (HTTP {status})" in printed.err and "GET /api/overview" in printed.err
    assert KEY not in printed.out + printed.err and "127.0.0.1" not in printed.err and "stolen" not in printed.err


def test_a_plain_http_address_on_this_machine_is_never_sent_through_a_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.acme.example:3128")
    local = build_proof.default_opener("http://127.0.0.1:8001/prod/").__self__.handlers
    assert not any(getattr(handler, "proxies", None) for handler in local)
    assert any(isinstance(handler, build_proof._RefuseRedirects) for handler in local)
    secure = build_proof.default_opener("https://acme-private.example/prod/").__self__.handlers
    assert any(isinstance(handler, build_proof._RefuseRedirects) for handler in secure)


@pytest.mark.parametrize("content", ["", "   \n", "two words\n"])
def test_a_key_file_that_does_not_hold_one_key_is_refused(tmp_path, content) -> None:
    path = tmp_path / "operator.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(build_proof.ProofError, match="alone, on one line"):
        build_proof.read_key(path)


def test_a_key_file_saved_with_a_byte_order_mark_reads_as_the_key_alone(tmp_path) -> None:
    path = tmp_path / "operator.key"
    path.write_text(KEY + "\r\n", encoding="utf-8-sig")
    assert build_proof.read_key(path) == KEY
    path.write_bytes(b"\xff\xfe not text")
    with pytest.raises(build_proof.ProofError, match="could not be read"):
        build_proof.read_key(path)


@pytest.mark.parametrize("spoil", [
    lambda o: o["totals"].__setitem__("calls", "Acme-Proj-Lighthouse"),
    lambda o: o.__setitem__("generated_at", "C:\\Users\\someone\\ledger"),
    lambda o: o["self_correction"].__setitem__("rate", "Acme-Proj-Harbour"),
    lambda o: o["stages"].__setitem__("observe", "Acme-Proj-Harbour"),
    lambda o: o["series"][0].__setitem__("observed", "Acme-Proj-Harbour"),
])
def test_text_where_a_number_was_expected_is_refused_and_never_repeated(spoil) -> None:
    hostile = _overview(SECRET_PROJECTS)
    spoil(hostile)
    with pytest.raises(build_proof.ProofError, match="not the shape the contract fixes") as refused:
        build_proof.private_section(hostile)
    assert not build_proof.leaks(str(refused.value), SECRET_PROJECTS) and "Users" not in str(refused.value)


@pytest.mark.parametrize("answer", [
    {},
    {"totals": {}},
    {key: value for key, value in _overview(SECRET_PROJECTS).items() if key != "series"},
    dict(_overview(SECRET_PROJECTS), series=[]),
    {key: value for key, value in _overview(SECRET_PROJECTS).items() if key != "stages"},
])
def test_an_answer_missing_what_the_contract_fixes_is_refused_not_defaulted(answer) -> None:
    # A missing series used to become "0 days observed": a zero nobody measured.
    with pytest.raises(build_proof.ProofError, match="not the shape the contract fixes"):
        build_proof.private_section(answer)


def test_a_stack_answering_only_part_of_the_contract_writes_nothing(tmp_path, key_file, capsys) -> None:
    out = tmp_path / "proof.json"
    with _Stack({"totals": {}}, _projects(SECRET_PROJECTS)) as stack:
        code = build_proof.main(["--private-endpoint", stack.url, "--key-file", str(key_file), "--out", str(out)])
    printed = capsys.readouterr()
    assert code == 2 and not out.exists() and "totals.calls is not a count" in printed.err


def test_a_stack_that_predates_self_correction_still_gives_its_totals() -> None:
    older = _overview(SECRET_PROJECTS)
    del older["self_correction"]
    assert build_proof.private_section(older)["self_correction"] is None


def test_the_false_alarm_rate_is_given_only_when_both_counts_are_of_the_same_calls() -> None:
    # Nothing refused: every label is on a would-refuse call, as reviewed is.
    observing = build_proof.private_section(_overview(SECRET_PROJECTS))
    assert (observing["reviewed"], observing["false_alarms"], observing["false_alarm_rate"]) == (150, 12, 0.08)
    # Refused calls in the window may carry labels too: false_alarms counts
    # them and reviewed cannot, so the rate is left out rather than let past 100%.
    enforcing = _overview(SECRET_PROJECTS)
    enforcing["totals"].update(calls=100, would_refuse=10, needs_review=8, false_alarms=5, refused=6)
    section = build_proof.private_section(enforcing)
    assert (section["reviewed"], section["false_alarms"], section["false_alarm_rate"]) == (2, None, None)
    nobody_reviewed = _overview(SECRET_PROJECTS)
    nobody_reviewed["totals"].update(needs_review=210, false_alarms=0)
    assert build_proof.private_section(nobody_reviewed)["false_alarm_rate"] is None, "None of none is not a rate"


def test_the_check_catches_a_name_in_any_case_the_key_and_an_absolute_path() -> None:
    assert build_proof.leaks({"note": "work on acme-proj-lighthouse"}, SECRET_PROJECTS) == ["a project name read from the private stack"]
    assert build_proof.leaks({"Acme-Proj-Harbour": 1}, SECRET_PROJECTS), "Keys are checked as well as values"
    assert build_proof.leaks(["x", {"y": KEY}], (), [KEY]) == ["the operator key"]
    for path in ("C:\\dev\\acme\\repo", "/home/acme/repo", "see ~/acme", "\\\\share\\acme"):
        assert build_proof.leaks({"where": path}, ()) == ["an absolute path"], path
    assert not build_proof.leaks({"path": "docs/evidence/BENCHMARK_2026-09-22-PILOT.md", "bucket": "unlabelled"}, ())
    names = build_proof.project_names(_overview(("unlabelled", "Acme-A")), {"projects": [{"project": ""}, "x"]})
    assert names == {"Acme-A"}, "The unlabelled bucket names no project, and malformed rows are skipped"


def test_the_committed_snapshot_passes_its_own_check() -> None:
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert not build_proof.leaks(committed, ())
