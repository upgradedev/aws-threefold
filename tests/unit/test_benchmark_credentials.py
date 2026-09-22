"""The login token reaches the agent process and nothing else, and --check-auth says what to do without showing it.

Every run here drives the real runner against a stand-in `claude` on PATH
(benchmark/fake_agents.py), which reaches no service. PATH is rebuilt from the
test's own folder plus the machine's folders that hold no `claude` or `codex`,
so the real executables can never be found instead. The token is made fresh
for each test and never written anywhere in the repository; the default token
file is pointed at a path that does not exist, so no test can read a real one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import credentials, fake_agents, harness, report, run  # noqa: E402


@pytest.fixture(autouse=True)
def _confined(tmp_path, monkeypatch):
    """No real token file, no CLAUDE.md refusal from the machine running the suite, and Codex's home in the test."""
    monkeypatch.setattr(credentials, "DEFAULT_TOKEN_FILE", tmp_path / "no-such-token-file")
    monkeypatch.setattr(harness, "claude_memory_above", lambda path: [])
    monkeypatch.delenv(credentials.TOKEN_ENV, raising=False)
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def _path_without_real_agents(bin_dir: Path) -> str:
    kept = [str(bin_dir)]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and not shutil.which("claude", path=entry) and not shutil.which("codex", path=entry):
            kept.append(entry)
    return os.pathsep.join(kept)


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    fake_agents.install(bin_dir, "claude")
    fake_agents.install(bin_dir, "codex")
    monkeypatch.setenv("PATH", _path_without_real_agents(bin_dir))
    assert Path(shutil.which("claude")).parent == bin_dir and Path(shutil.which("codex")).parent == bin_dir
    return bin_dir


@pytest.fixture
def token(tmp_path):
    value = "acme-bench-fixture-" + secrets.token_hex(24)
    path = tmp_path / "secret" / "claude-oauth-token"
    path.parent.mkdir()
    path.write_text(value + "\n", encoding="utf-8")
    return value, path


_BASE64_RUN = re.compile(rb"[A-Za-z0-9+/_-]{24,}")


def _decoded_base64(data: bytes):
    """Every stretch of base64 in the data, decoded at each of its four starting points, so a copy is found wherever it sits."""
    for match in _BASE64_RUN.finditer(data):
        run_text = match.group(0).replace(b"-", b"+").replace(b"_", b"/")
        for start in range(4):
            part = run_text[start:]
            part = part[: len(part) - len(part) % 4]
            try:
                yield base64.b64decode(part)
            except ValueError:
                continue


def _files_holding(root: Path, needle: str):
    """Every path under root holding the token in a shape a leak could take, written here apart from the harness's own
    search so it checks that search rather than repeating it: in a name (as text or hex), or in the contents as UTF-8,
    UTF-16, JSON escapes, hex or base64."""
    raw = needle.encode("utf-8")
    escaped = "".join(f"\\u{ord(character):04x}" for character in needle)
    shapes = [raw, needle.encode("utf-16-le"), needle.encode("utf-16-be"), escaped.encode(),
              escaped.upper().replace("\\U", "\\u").encode(), raw.hex().encode(), raw.hex().upper().encode()]
    holding = []
    for path in Path(root).rglob("*"):
        if needle in path.name or raw.hex() in path.name:
            holding.append(path)
        elif path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(shape in data for shape in shapes) or any(raw in decoded for decoded in _decoded_base64(data)):
                holding.append(path)
    return holding


def _rows(results_dir: Path):
    return [json.loads(line) for path in sorted(Path(results_dir).glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(tmp_path, *extra):
    return run.main(["--tasks", "orders-s3-archive", "--reps", "1", "--retry-pause", "0",
                     "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results"), *extra])


# --- the token file ---------------------------------------------------------------------

def test_a_token_file_is_read_alone_and_the_token_is_hidden_from_repr(tmp_path):
    path = tmp_path / "t"
    path.write_bytes(b"\xef\xbb\xbfacme-bench-fixture-0123456789abcdef\r\n")
    credential = credentials.read_token_file(path)
    assert credential.reveal() == "acme-bench-fixture-0123456789abcdef"
    assert "acme-bench-fixture" not in repr(credential) and "acme-bench-fixture" not in str(credential)
    assert "<hidden>" in repr(credential)


@pytest.mark.parametrize("content, problem", [
    ("", "is empty"),
    ("acme-bench-fixture-0123456789\nsecond-line", "more than one word or line"),
    ("export TOKEN=acme-bench-fixture-0123456789", "more than one word or line"),
    ("short", "too short"),
])
def test_a_token_file_that_holds_anything_else_is_refused_without_quoting_it(tmp_path, content, problem):
    path = tmp_path / "t"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(credentials.TokenFileError) as caught:
        credentials.read_token_file(path)
    assert problem in str(caught.value)
    assert "acme-bench-fixture" not in str(caught.value)


def test_the_default_token_file_is_used_only_when_it_exists(tmp_path):
    default = tmp_path / "default-token"
    assert credentials.resolve_token_file(None, default) is None
    default.write_text("x", encoding="utf-8")
    assert credentials.resolve_token_file(None, default) == default
    assert credentials.resolve_token_file(tmp_path / "asked", default) == tmp_path / "asked"


# --- where the token may and may not go ---------------------------------------------------

def test_only_the_agent_environment_of_a_fresh_config_run_holds_the_token(tmp_path):
    credential = credentials.Credential("acme-bench-fixture-" + "a" * 30)
    host = {"PATH": "/bin", credentials.TOKEN_ENV: "inherited-from-a-shell-0123456789", "HOME": "/owner"}
    agent = harness.agent_environment(host, tmp_path / "run", "fresh-config", credential)
    assert agent[credentials.TOKEN_ENV] == credential.reveal()
    assert Path(agent["CLAUDE_CONFIG_DIR"]).is_relative_to(tmp_path / "run")
    assert Path(agent["HOME"]).is_relative_to(tmp_path / "run")
    assert credentials.TOKEN_ENV not in harness.base_environment(host, tmp_path / "run")
    assert credentials.TOKEN_ENV not in harness.LocalServer(tmp_path / "run", port=45679).environment(host)
    assert credentials.TOKEN_ENV not in harness.agent_environment(host, tmp_path / "run", "user-config")
    with pytest.raises(ValueError):
        harness.agent_environment(host, tmp_path / "run", "user-config", credential)
    with pytest.raises(ValueError):
        harness.agent_environment(host, tmp_path / "run", "user-config", credential, "codex")
    with pytest.raises(ValueError):
        harness.agent_environment(host, tmp_path / "run", "fresh-config")


def test_the_hook_wrapper_removes_the_token_before_the_hook_starts_and_logs_each_call(tmp_path):
    """Claude Code 2.1.220 already strips it from its subprocesses; the wrapper does not rely on that."""
    probe = tmp_path / "probe_hook.py"
    probe.write_text("import json, os, sys\n"
                     f"print(json.dumps({{'token': {credentials.TOKEN_ENV!r} in os.environ, 'argv': sys.argv[1:]}}))\n",
                     encoding="utf-8")
    wrapper = harness.write_hook_wrapper(tmp_path / "run", probe, agent="codex")
    env = dict(os.environ, **{credentials.TOKEN_ENV: "acme-bench-fixture-" + "b" * 30})
    completed = subprocess.run([sys.executable, str(wrapper)], input=b"{}", capture_output=True, env=env, timeout=60)
    answer = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    assert answer == {"token": False, "argv": ["--agent", "codex"]}
    log = (tmp_path / "run" / harness.HOOK_LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["decided"] for line in log] == [True]


def test_the_token_file_is_denied_to_the_agent_by_its_path_and_its_contents_are_in_no_rule(tmp_path):
    token_file = tmp_path / "secret" / "claude-oauth-token"
    denied = harness.denied_tools(tmp_path / "home", [token_file])
    assert f"Read({harness.rule_path(token_file)})" in denied and f"Edit({harness.rule_path(token_file)})" in denied


# --- whole runs with a stand-in claude ----------------------------------------------------

def test_the_token_reaches_the_agent_only_and_no_output_row_or_file_of_a_run_holds_it(tmp_path, fake_bin, token, capsys):
    value, path = token
    code = _run(tmp_path, "--conditions", "none,threefold", "--token-file", str(path))
    printed = capsys.readouterr()
    assert code == 0, printed.err

    agent_calls = fake_agents.calls(fake_bin, "claude")
    assert len(agent_calls) == 2
    for call in agent_calls:
        assert call["token_set"] and call["token_sha256"] == hashlib.sha256(value.encode()).hexdigest()
        assert not call["token_in_argv"]
        assert Path(call["claude_config_dir"]).is_relative_to(tmp_path / "work")
        assert Path(call["home"]).is_relative_to(tmp_path / "work")
        assert Path(call["cwd"]).name == "repo"

    rows = _rows(tmp_path / "results")
    assert sorted(row["condition"] for row in rows) == ["none", "threefold"]
    for row in rows:
        assert row["auth"] == "token-file" and row["agent"] == "claude-code"
        assert row["token_found_in"] == []
        assert row["isolation"]["mode"] == "fresh-config"
        assert value not in json.dumps(row)
        assert str(path) not in json.dumps(row) and harness.forward(path) not in json.dumps(row)

    assert value not in printed.out and value not in printed.err
    assert _files_holding(tmp_path / "work", value) == []
    assert _files_holding(tmp_path / "results", value) == []
    settings = (tmp_path / "work" / "orders-s3-archive--none--r1" / "agent-settings.json").read_text(encoding="utf-8")
    assert f"Read({harness.rule_path(path)})" in settings
    threefold_repo = tmp_path / "work" / "orders-s3-archive--threefold--r1" / "repo"
    assert value not in (threefold_repo / ".threefold.json").read_text(encoding="utf-8")
    assert value not in (threefold_repo / ".claude" / "settings.local.json").read_text(encoding="utf-8")


def test_a_token_the_agent_spilled_is_scrubbed_from_every_file_and_never_reaches_a_row(tmp_path, fake_bin, token, capsys):
    """The stand-in echoes its token into the transcript, its stderr, a committed file and its configuration folder."""
    value, path = token
    fake_agents.set_behaviour(fake_bin, "claude", "leak")
    code = _run(tmp_path, "--conditions", "none", "--token-file", str(path))
    printed = capsys.readouterr()
    assert code == 0, printed.err
    (row,) = _rows(tmp_path / "results")
    assert value not in json.dumps(row)
    found = row["token_found_in"]
    assert "transcript.jsonl" in found and "agent-stderr.txt" in found and "claude-config/.credentials.json" in found
    assert any(item.startswith("repo/.git") and "deleted" in item for item in found)
    assert not (tmp_path / "work" / "orders-s3-archive--none--r1" / "repo" / ".git").exists()
    assert _files_holding(tmp_path / "work", value) == []
    assert _files_holding(tmp_path / "results", value) == []
    assert value not in printed.out and value not in printed.err


def test_a_token_hidden_in_names_encodings_and_a_hard_link_is_found_and_the_token_file_is_left_alone(
        tmp_path, fake_bin, token, capsys):
    """The stand-in names files and folders after the token, writes it as UTF-16, hex, base64 and JSON escapes, and
    hard-links the owner's token file into the repository, as the reviewer's hostile agent did."""
    value, path = token
    probe = tmp_path / "probe-link"
    try:
        os.link(path, probe)
        probe.unlink()
        links = True
    except OSError:
        links = False
    fake_agents.set_behaviour(fake_bin, "claude", "hide", link_to=str(path))
    code = _run(tmp_path, "--conditions", "none", "--token-file", str(path))
    printed = capsys.readouterr()
    assert code == 0, printed.err
    assert path.read_text(encoding="utf-8") == value + "\n", "the owner's token file was changed"
    (row,) = _rows(tmp_path / "results")
    found = row["token_found_in"]
    run_dir = tmp_path / "work" / "orders-s3-archive--none--r1"
    for expected in ("repo/notes.txt", "repo/b64.txt", "transcript.jsonl"):
        assert expected in found
    assert any(item.startswith("repo/leak-<redacted>.txt") and "renamed" in item for item in found)
    assert any(item.startswith("repo/hex-<redacted>.txt") and "renamed" in item for item in found)
    assert any(item.startswith("claude-config/cache-<redacted>") and "renamed" in item for item in found)
    if links:
        assert "repo/copy.txt (a hard link: this name was removed and the file behind it left alone)" in found
        assert not (run_dir / "repo" / "copy.txt").exists() and os.stat(path).st_nlink == 1
    assert (run_dir / "repo" / "leak-REDACTED.txt").is_file() and (run_dir / "claude-config" / "cache-REDACTED").is_dir()
    serialised = json.dumps(row)
    assert value not in serialised and value.encode().hex() not in serialised
    assert _files_holding(tmp_path / "work", value) == []
    assert _files_holding(tmp_path / "results", value) == []
    assert value not in printed.out and value not in printed.err


def test_scrubbing_never_writes_through_a_hard_link_to_a_file_outside_the_run(tmp_path):
    secret = "acme-bench-fixture-" + secrets.token_hex(24)
    owner = tmp_path / "owner" / "claude-oauth-token"
    owner.parent.mkdir()
    owner.write_text(secret + "\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    (run_dir / "repo").mkdir(parents=True)
    try:
        os.link(owner, run_dir / "repo" / "copy.txt")
    except OSError as error:
        pytest.skip(f"this file system makes no hard links: {error}")
    (run_dir / "plain.txt").write_text(f"token={secret}\n", encoding="utf-8")
    found = harness.scrub_secret(run_dir, secret)
    assert found == ["plain.txt", "repo/copy.txt (a hard link: this name was removed and the file behind it left alone)"]
    assert owner.read_text(encoding="utf-8") == secret + "\n" and os.stat(owner).st_nlink == 1
    assert not (run_dir / "repo" / "copy.txt").exists()
    assert (run_dir / "plain.txt").read_text(encoding="utf-8") == "token=<redacted>\n"


def _link_folder(link: Path, target: Path) -> bool:
    """A folder link an agent could make without special rights: a symbolic link, or a junction on Windows."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return completed.returncode == 0 and link.exists()
    return False


def test_a_link_named_after_the_token_is_removed_and_what_it_points_to_is_left_alone(tmp_path):
    secret = "acme-bench-fixture-" + secrets.token_hex(24)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text(secret, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    link = run_dir / f"link-{secret}"
    if not _link_folder(link, outside):
        pytest.skip("this machine lets no folder link be made without special rights")
    found = harness.scrub_secret(run_dir, secret)
    assert found == ["link-<redacted> (a link whose name or target held it: the link was removed)"]
    assert not os.path.lexists(link)
    assert (outside / "kept.txt").read_text(encoding="utf-8") == secret


def test_every_text_shape_of_the_token_is_redacted_from_what_is_recorded():
    secret = "acme-bench-fixture-" + "d" * 40
    sanitise = harness.Sanitiser(secrets=[secret])
    hex_name = secret.encode().hex()
    assert sanitise(f"repo/hex-{hex_name}.txt") == "repo/hex-<redacted>.txt"
    assert harness.redact({"files_changed": [f"leak-{secret}.txt", f"hex-{hex_name}"]}, secret) == {
        "files_changed": ["leak-<redacted>.txt", "hex-<redacted>"]}
    for prefix in (b"", b"x", b"xy"):
        encoded = base64.b64encode(prefix + secret.encode() + b"tail").decode()
        assert "<redacted>" in sanitise(encoded, 1000)


def test_a_token_exported_in_the_shell_is_not_used_and_the_owner_is_told(tmp_path, fake_bin, monkeypatch, capsys):
    monkeypatch.setenv(credentials.TOKEN_ENV, "inherited-from-a-shell-0123456789")
    code = _run(tmp_path, "--conditions", "none", "--isolation", "user-config")
    printed = capsys.readouterr()
    assert code == 0
    assert "is set in this shell and is not used" in printed.err
    (call,) = fake_agents.calls(fake_bin, "claude")
    assert call["token_set"] is False
    (row,) = _rows(tmp_path / "results")
    assert row["auth"] == "machine-login" and "token_found_in" not in row


def test_a_token_file_asked_for_that_is_missing_stops_before_any_run(tmp_path, fake_bin, capsys):
    code = _run(tmp_path, "--token-file", str(tmp_path / "nowhere"))
    assert code == 2
    assert "claude setup-token" in capsys.readouterr().err
    assert fake_agents.calls(fake_bin, "claude") == [] and not (tmp_path / "results").exists()


def test_a_dry_run_names_the_token_file_but_never_the_token(tmp_path, fake_bin, token, capsys):
    value, path = token
    assert _run(tmp_path, "--dry-run", "--token-file", str(path)) == 0
    printed = capsys.readouterr()
    assert str(path) in printed.out and value not in printed.out + printed.err
    assert "login token-file" in printed.out and "isolation fresh-config" in printed.out


# --- --check-auth ---------------------------------------------------------------------------

def _check(tmp_path, *extra):
    return run.main(["--check-auth", "--work-root", str(tmp_path / "work" / "unused"), *extra])


def test_check_auth_says_ok_with_a_working_token_file_and_leaves_nothing_behind(tmp_path, fake_bin, token, capsys):
    value, path = token
    assert _check(tmp_path, "--token-file", str(path)) == 0
    printed = capsys.readouterr()
    assert printed.out.startswith("check-auth: ok (token-file)")
    assert value not in printed.out + printed.err
    (call,) = fake_agents.calls(fake_bin, "claude")
    assert call["token_sha256"] == hashlib.sha256(value.encode()).hexdigest() and not call["token_in_argv"]
    assert "--max-turns" in call["argv"] and call["claude_config_dir"]
    assert not list((tmp_path / "work").glob("threefold-auth-*"))


@pytest.mark.parametrize("mode, status, step", [
    ("expired", "expired", "run `claude setup-token` again"),
    ("usage_limit", "limited", "wait until the limit resets"),
    ("overloaded", "limited", "wait until the limit resets"),
])
def test_check_auth_names_what_went_wrong_with_the_token_and_the_next_step(tmp_path, fake_bin, token, capsys, mode, status, step):
    value, path = token
    fake_agents.set_behaviour(fake_bin, "claude", mode)
    assert _check(tmp_path, "--token-file", str(path)) == 1
    printed = capsys.readouterr()
    assert printed.out.startswith(f"check-auth: {status} (token-file)")
    assert f"next step: {step}" in printed.out
    assert value not in printed.out + printed.err


def test_check_auth_without_a_token_file_or_a_login_says_missing_and_how_to_make_one(tmp_path, fake_bin, capsys):
    fake_agents.set_behaviour(fake_bin, "claude", "not_logged_in")
    assert _check(tmp_path) == 1
    out = capsys.readouterr().out
    assert out.startswith("check-auth: missing (machine-login)")
    assert "claude setup-token" in out and str(credentials.DEFAULT_TOKEN_FILE) in out


def test_check_auth_tests_the_login_the_chosen_isolation_would_use(tmp_path, fake_bin, token, capsys):
    """With --isolation user-config the matrix leaves the token file unused, so the check must too."""
    value, path = token
    assert _check(tmp_path, "--token-file", str(path), "--isolation", "user-config") == 0
    printed = capsys.readouterr()
    assert printed.out.startswith("check-auth: ok (machine-login)") and value not in printed.out + printed.err
    (call,) = fake_agents.calls(fake_bin, "claude")
    assert call["token_set"] is False and call["claude_config_dir"] is None


def test_check_auth_with_fresh_config_and_no_token_file_calls_nothing(tmp_path, fake_bin, capsys):
    assert _check(tmp_path, "--isolation", "fresh-config") == 1
    out = capsys.readouterr().out
    assert out.startswith("check-auth: missing (token-file): fresh-config needs a token file") and "claude setup-token" in out
    assert fake_agents.calls(fake_bin, "claude") == []


def test_check_auth_with_a_missing_or_malformed_token_file_calls_nothing(tmp_path, fake_bin, capsys):
    assert _check(tmp_path, "--token-file", str(tmp_path / "nowhere")) == 1
    assert capsys.readouterr().out.startswith("check-auth: missing (token-file): there is no token file")
    bad = tmp_path / "bad-token"
    bad.write_text("two words", encoding="utf-8")
    assert _check(tmp_path, "--token-file", str(bad)) == 1
    assert "more than one word" in capsys.readouterr().out
    assert fake_agents.calls(fake_bin, "claude") == []


@pytest.mark.parametrize("mode, status, code", [
    ("ok", "ok", 0), ("not_logged_in", "missing", 1), ("usage_limit", "limited", 1), ("expired", "expired", 1),
])
def test_check_auth_for_codex_reads_its_login_and_makes_one_call(tmp_path, fake_bin, capsys, mode, status, code):
    fake_agents.set_behaviour(fake_bin, "codex", mode)
    assert _check(tmp_path, "--agent", "codex") == code
    out = capsys.readouterr().out
    assert out.startswith(f"check-auth: {status} (machine-login)")
    made = fake_agents.calls(fake_bin, "codex")
    assert made[0]["argv"][:2] == ["login", "status"]
    assert all(call["codex_home"] == str(tmp_path / "codex-home") for call in made)
    if mode != "not_logged_in":
        assert made[1]["argv"][0] == "exec" and "--ignore-user-config" in made[1]["argv"]


def test_a_row_says_how_the_agent_logged_in_and_nothing_more():
    options = harness.AgentOptions(agent="claude-code")
    plan = harness.RunPlan(run_id="r", work_root=Path("w"), options=options,
                           credential=credentials.Credential("acme-bench-fixture-" + "c" * 30, Path("t")))
    assert plan.auth == "token-file" and "acme-bench-fixture" not in repr(plan)
    assert harness.RunPlan(run_id="r", work_root=Path("w"), options=options).auth == "machine-login"
    assert harness.RunPlan(run_id="r", work_root=Path("w"), options=harness.AgentOptions(agent="scripted")).auth == "none"
    assert report.is_scripted({"agent": "scripted"})
