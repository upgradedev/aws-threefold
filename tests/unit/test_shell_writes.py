"""What a shell command writes, read the way the shell would run it.

Each test names one route and says what the module must find on it: the file
written and either the text written or UNKNOWN (None). The judging lives in the
boundary guard and has its own tests; these pin the reading, because a route
this module misreads is a route the gate cannot see, whatever the rules say.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import time

import pytest

from threefold.domain.shell_writes import (
    MAX_WRITES,
    UNKNOWN,
    analyse,
    diff_writes,
    is_governance_path,
    node_writes,
    python_writes,
    sed_added_text,
)

DOMAIN = "src/domain/acme_user.py"


def _writes(command: str, cwd: str = ""):
    return [(write.target, write.content) for write in analyse(command, cwd).writes if not write.deletes]


# --- redirections and heredocs -------------------------------------------------------

def test_the_audited_heredoc_is_read_with_its_body_as_the_content() -> None:
    command = "cat > src/domain/acme_user.py <<'EOF'\nimport boto3\nEOF"
    assert _writes(command) == [(DOMAIN, "import boto3")]


def test_a_heredoc_written_before_the_redirect_is_the_same_write() -> None:
    assert _writes("cat <<EOF > src/domain/acme_user.py\nimport boto3\nEOF") == [(DOMAIN, "import boto3")]


def test_a_dash_heredoc_strips_the_leading_tabs_from_its_body() -> None:
    assert _writes("cat <<-EOF > src/domain/acme_user.py\n\timport boto3\n\tEOF") == [(DOMAIN, "import boto3")]


@pytest.mark.parametrize("operator", [">", ">>", "1>", "&>", ">|"])
def test_every_output_redirection_carries_what_echo_printed(operator: str) -> None:
    assert _writes(f"echo 'import boto3' {operator} {DOMAIN}") == [(DOMAIN, "import boto3\n")]


def test_printf_is_read_with_its_escapes_expanded() -> None:
    assert _writes(f"printf 'import boto3\\nx = 1\\n' > {DOMAIN}") == [(DOMAIN, "import boto3\nx = 1\n")]


def test_tee_writes_what_arrives_on_its_input_and_append_changes_nothing_about_that() -> None:
    assert _writes(f"echo 'import boto3' | tee -a {DOMAIN}") == [(DOMAIN, "import boto3\n")]
    assert _writes(f"tee {DOMAIN} <<< 'import boto3'") == [(DOMAIN, "import boto3\n")]


def test_a_program_whose_output_cannot_be_known_writes_unknown_content() -> None:
    assert _writes(f"curl -s https://acme.example/x.py > {DOMAIN}") == [(DOMAIN, UNKNOWN)]
    assert _writes(f"cat /tmp/acme.py > {DOMAIN}") == [(DOMAIN, UNKNOWN)]


def test_standard_error_joined_to_output_is_not_a_write_to_a_file_named_1() -> None:
    assert _writes("pytest -q 2>&1 | tail -5") == []


@pytest.mark.parametrize("sink", ["/dev/null", "/dev/stderr", "NUL"])
def test_the_null_device_and_the_streams_are_not_files(sink: str) -> None:
    assert _writes(f"make test > {sink}") == []


def test_a_quoted_greater_than_sign_is_text_not_a_redirect() -> None:
    assert _writes("echo '>' src/domain/acme_user.py") == []
    assert _writes('python -c "print(1 > 0)"') == []


def test_a_heredoc_that_feeds_a_commit_message_writes_nothing() -> None:
    command = 'git commit -m "$(cat <<\'EOF\'\nfix: acme rounding\n\nimport boto3 is mentioned here\nEOF\n)"'
    assert _writes(command) == []


# --- where the command stands ---------------------------------------------------------

def test_a_cd_prefix_moves_the_relative_target_with_it() -> None:
    assert _writes("cd src/domain && echo 'import boto3' > acme_user.py") == [(DOMAIN, "import boto3\n")]


def test_a_subshell_keeps_its_cd_to_itself() -> None:
    command = "(cd src/domain; echo 'import boto3' > acme_user.py) && echo ok > notes.txt"
    assert _writes(command) == [(DOMAIN, "import boto3\n"), ("notes.txt", "ok\n")]


def test_the_working_directory_the_caller_names_is_where_relative_paths_start() -> None:
    assert _writes("echo 'import boto3' > acme_user.py", cwd="src/domain") == [(DOMAIN, "import boto3\n")]


def test_a_script_handed_to_bash_is_read_as_a_command_of_its_own() -> None:
    assert _writes(f"bash -c \"echo 'import boto3' > {DOMAIN}\"") == [(DOMAIN, "import boto3\n")]


def test_every_command_of_a_chain_is_read_whatever_joins_them() -> None:
    command = "true && echo a > a.txt || echo b > b.txt; echo c > c.txt\necho d > d.txt | cat"
    assert [target for target, _ in _writes(command)] == ["a.txt", "b.txt", "c.txt", "d.txt"]


# --- editors in place ------------------------------------------------------------------

def test_sed_in_place_writes_its_replacement_text() -> None:
    assert _writes(f"sed -i 's/^/import boto3\\n/' {DOMAIN}") == [(DOMAIN, "import boto3\n")]


def test_sed_insert_writes_the_inserted_line() -> None:
    assert sed_added_text("1i import boto3") == "import boto3"


def test_sed_reading_its_script_from_a_file_writes_unknown_content() -> None:
    assert _writes(f"sed -i -f /tmp/acme.sed {DOMAIN}") == [(DOMAIN, UNKNOWN)]


def test_sed_without_in_place_writes_nothing() -> None:
    assert _writes(f"sed -n '1,5p' {DOMAIN}") == []


def test_perl_in_place_with_a_plain_substitution_writes_its_replacement() -> None:
    assert _writes(f"perl -pi -e 's/Acme Ltd/Acme Inc/g' {DOMAIN}") == [(DOMAIN, "Acme Inc")]


def test_perl_in_place_running_any_other_code_writes_unknown_content() -> None:
    assert _writes(f"perl -pi -e 'print \"import boto3\\n\" if $. == 1' {DOMAIN}") == [(DOMAIN, UNKNOWN)]


# --- copies, moves and links --------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        f"cp /tmp/acme.py {DOMAIN}",
        f"mv /tmp/acme.py {DOMAIN}",
        f"install -m 644 /tmp/acme.py {DOMAIN}",
        f"ln -s /tmp/acme.py {DOMAIN}",
        f"rsync -a /tmp/acme.py {DOMAIN}",
        f"dd if=/tmp/acme.py of={DOMAIN}",
    ],
)
def test_a_copy_writes_its_destination_with_unknown_content(command: str) -> None:
    assert (DOMAIN, UNKNOWN) in _writes(command)


def test_a_copy_into_a_directory_writes_the_file_of_the_same_name_inside_it() -> None:
    assert _writes("cp /tmp/acme_user.py src/domain/") == [(DOMAIN, UNKNOWN)]


def test_a_copy_into_what_may_be_a_directory_is_judged_both_ways() -> None:
    """Only the file system knows whether src/domain is a directory, so both
    readings are reported and a rule on either path sees the write."""
    assert _writes("cp /tmp/acme_user.py src/domain") == [("src/domain", UNKNOWN), (DOMAIN, UNKNOWN)]


def test_a_move_also_removes_its_source() -> None:
    deleted = [(w.target, w.deletes) for w in analyse(f"mv {DOMAIN} src/domain/renamed.py").writes if w.deletes]
    assert deleted == [(DOMAIN, True)]


def test_a_copy_of_a_file_the_same_command_wrote_carries_what_was_written() -> None:
    command = f"echo 'import boto3' > /tmp/acme.py && cp /tmp/acme.py {DOMAIN}"
    assert (DOMAIN, "import boto3\n") in _writes(command)


# --- patches ----------------------------------------------------------------------------------

PATCH = "--- a/src/domain/acme_user.py\n+++ b/src/domain/acme_user.py\n@@ -1,1 +1,2 @@\n x = 1\n+import boto3\n"


def test_git_apply_with_a_heredoc_writes_the_added_lines() -> None:
    assert _writes(f"git apply <<'EOF'\n{PATCH}EOF") == [(DOMAIN, "import boto3")]


def test_patch_with_a_heredoc_writes_the_added_lines() -> None:
    assert _writes(f"patch -p1 <<'EOF'\n{PATCH}EOF") == [(DOMAIN, "import boto3")]


def test_a_patch_read_from_a_file_writes_files_nobody_can_name() -> None:
    writes = analyse("git apply fix.patch").writes
    assert [(w.target, w.content, w.route) for w in writes] == [(None, UNKNOWN, "git apply")]


def test_a_removed_line_is_not_what_a_patch_writes() -> None:
    patch = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n-import boto3\n x = 1\n"
    assert diff_writes(patch) == [("x.py", "", False)]


def test_git_apply_that_only_checks_writes_nothing() -> None:
    assert _writes(f"git apply --check <<'EOF'\n{PATCH}EOF") == []


# --- code handed to an interpreter ------------------------------------------------------------

def test_python_opening_a_literal_path_for_writing_writes_the_literal() -> None:
    assert _writes(f"python -c \"open('{DOMAIN}','w').write('import boto3')\"") == [(DOMAIN, "import boto3")]


def test_python_appending_through_a_with_block_is_read_too() -> None:
    code = f"with open('{DOMAIN}', 'a') as f:\n    f.write('import boto3\\n')"
    assert python_writes(code) == [(DOMAIN, "import boto3\n", "python open()", False)]


def test_path_write_text_writes_its_literal() -> None:
    command = f"python3 -c \"from pathlib import Path; Path('{DOMAIN}').write_text('import boto3')\""
    assert _writes(command) == [(DOMAIN, "import boto3")]


def test_python_writing_something_computed_writes_unknown_content() -> None:
    command = f"python -c \"open('{DOMAIN}','w').write(open('/tmp/acme').read())\""
    assert _writes(command) == [(DOMAIN, UNKNOWN)]


def test_python_opening_a_file_to_read_writes_nothing() -> None:
    assert _writes(f"python -c \"print(open('{DOMAIN}').read())\"") == []


def test_python_reading_its_code_from_a_heredoc_is_read_the_same_way() -> None:
    command = f"python - <<'EOF'\nopen('{DOMAIN}', 'w').write('import boto3')\nEOF"
    assert _writes(command) == [(DOMAIN, "import boto3")]


def test_a_string_replace_in_a_one_liner_is_not_a_file_move() -> None:
    """Path.rename and Path.replace take one argument and str.replace takes two.
    Reading the second as the first made `.replace('.claude/settings.json', '')`
    a write to the hooks' settings, refused for text nobody wrote to a file."""
    assert python_writes("print(line.replace('.claude/settings.json', ''))") == []
    moved = python_writes("from pathlib import Path\nPath('/tmp/a.py').replace('src/domain/a.py')")
    assert ("src/domain/a.py", UNKNOWN, "python Path.replace()", False) in moved


def test_node_write_file_sync_with_literals_writes_the_literal() -> None:
    assert node_writes("require('fs').writeFileSync('src/domain/a.ts', 'import axios from \"axios\"')") == [
        ("src/domain/a.ts", 'import axios from "axios"', "node writeFileSync()", False)
    ]


def test_node_writing_a_variable_writes_unknown_content() -> None:
    assert _writes("node -e \"require('fs').writeFileSync('src/domain/a.ts', body)\"") == [("src/domain/a.ts", UNKNOWN)]


def test_running_a_script_file_is_not_read_into() -> None:
    """The documented price: a script on disk can write anything, and nothing
    here pretends to see inside it. Refusing it would refuse every test run."""
    assert _writes("python scripts/acme_build.py") == []


# --- the hooks' own files and the ways around them -----------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.json", ".codex/config.toml",
        ".agents/hooks.json", ".threefold.json", ".git/hooks/pre-commit", "./.git/hooks/post-merge",
        "nested/checkout/.claude/settings.json", ".threefold/rules.json", ".CLAUDE/Settings.JSON",
    ],
)
def test_the_files_that_decide_whether_hooks_run_are_governance_paths(path: str) -> None:
    assert is_governance_path(path) is True


@pytest.mark.parametrize("path", [".claude/agents/reviewer.md", ".gitignore", "src/settings.json", "docs/hooks.json", ".github/workflows/ci.yml"])
def test_files_beside_them_are_not(path: str) -> None:
    assert is_governance_path(path) is False


def test_removing_a_whole_settings_directory_removes_the_files_with_it() -> None:
    assert is_governance_path(".claude", deletes=True) is True
    assert is_governance_path(".claude") is False


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m wip",
        "git commit --no-veri -m wip",
        "git commit -n -m wip",
        "git commit -anm wip",
        "git -c core.hooksPath=/dev/null commit -m wip",
        "git config core.hooksPath /tmp/acme-none",
        "git config --local core.hooksPath .acme",
        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/tmp'\" git commit -m wip",
    ],
)
def test_every_way_of_telling_git_to_skip_the_hooks_is_found(command: str) -> None:
    assert analyse(command).tampering


@pytest.mark.parametrize(
    "command",
    ["git commit -m 'no verify needed'", "git config --get core.hooksPath", "git commit -m -n", "git log -n 5"],
)
def test_commands_that_only_mention_the_hooks_are_not_tampering(command: str) -> None:
    assert analyse(command).tampering == []


# --- bounded on hostile input ----------------------------------------------------------------------

HOSTILE = {
    "a million characters of echo": "echo " + "a" * 1_000_000,
    "unclosed groups": "(" * 200_000,
    "unclosed substitutions": "$(" * 200_000,
    "unclosed parameter expansions": "${" * 200_000,
    "arithmetic that never closes": "$((" * 150_000,
    "a pipeline of a quarter of a million stages": "a | " * 250_000,
    "a heredoc opener on every line": "cat <<EOF\n" * 50_000,
    "a redirect on every word": "echo x > a " * 100_000,
    "ANSI-C escapes": "$'" + "\\x41" * 200_000,
    "twenty thousand patch entries": "git apply <<'EOF'\n" + "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n" * 20_000 + "EOF",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_input_is_read_in_bounded_time(name: str) -> None:
    """Every shape here was quadratic or unbounded in some first draft. A second
    per million characters is linear with room; quadratic would be minutes."""
    started = time.perf_counter()
    analysis = analyse(HOSTILE[name])
    assert time.perf_counter() - started < 5.0
    assert len(analysis.writes) <= MAX_WRITES


def test_a_command_padded_past_the_cap_says_it_was_not_read_to_the_end() -> None:
    padded = "X=" + "a" * 40_000 + f" python -c \"open('{DOMAIN}','w').write('import boto3')\""
    assert analyse(padded).truncated is True


def test_a_long_command_that_cannot_write_is_not_marked_unread() -> None:
    assert analyse("echo " + "a" * 40_000).truncated is False
