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
    display,
    is_governance_path,
    node_writes,
    pattern_is_governance,
    pattern_matches_glob,
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
        "nested/checkout/.claude/settings.json", ".threefold/rules.json", ".CLAUDE/Settings.JSON", ".git/config",
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


# --- what the shell decides when it runs ----------------------------------------------------------

def _patterns(command: str, cwd: str = ""):
    return [(display(w.target), w.content, w.pattern) for w in analyse(command, cwd).writes if not w.deletes]


@pytest.mark.parametrize(
    "command",
    [
        f'M=boto3; echo "import $M" > {DOMAIN}',
        f'echo "import ${{M}}" > {DOMAIN}',
        f'echo "import $(echo boto3)" > {DOMAIN}',
        f"echo import `echo boto3` > {DOMAIN}",
        f"echo import $((1)) > {DOMAIN}",
        f"echo import boto* > {DOMAIN}",
        f"echo import {{boto3,x}} > {DOMAIN}",
        f"cat > {DOMAIN} <<EOF\nimport $M\nEOF",
        f"cat > {DOMAIN} <<EOF\n$(printf 'import boto3')\nEOF",
        f"cat > {DOMAIN} <<EOF\nimport `echo boto3`\nEOF",
        f"python -c \"open('{DOMAIN}','w').write('import $M')\"",
        f'tee {DOMAIN} <<< "import $M"',
    ],
)
def test_content_with_an_expansion_in_it_is_unknown_not_the_text_it_is_spelled_with(command: str) -> None:
    """Bash wrote `import boto3` for the first of these while the content was read as `import $M`."""
    assert _writes(command) == [(DOMAIN, UNKNOWN)]


def test_a_quoted_heredoc_is_written_as_it_is_spelled_and_an_escaped_dollar_stays_a_dollar() -> None:
    assert _writes(f"cat > {DOMAIN} <<'EOF'\nprice = '$5'\nEOF") == [(DOMAIN, "price = '$5'")]
    assert _writes(f"cat > {DOMAIN} <<EOF\nprice = '\\$5'\nEOF") == [(DOMAIN, "price = '$5'")]


def test_single_quotes_keep_a_dollar_sign_literal() -> None:
    assert _writes(f"echo 'cost = $total' > {DOMAIN}") == [(DOMAIN, "cost = $total\n")]


@pytest.mark.parametrize(
    "command, shape",
    [
        ("D=src/domain; echo x > $D/acme_user.py", "${...}/acme_user.py"),
        ("D=src/domain; cd $D && echo x > acme_user.py", "${...}/acme_user.py"),
        ("echo x > src/d?main/acme_user.py", "src/d?main/acme_user.py"),
        ("echo x > src/domai[n]/acme_user.py", "src/domai[n]/acme_user.py"),
        ("echo x > src/*/acme_user.py", "src/*/acme_user.py"),
        ("echo x | tee src/{domain,app}/acme_user.py", "src/${...}/acme_user.py"),
    ],
)
def test_a_target_with_an_expansion_or_a_glob_is_a_shape_not_a_path(command: str, shape: str) -> None:
    assert _patterns(command) == [(shape, "x\n", True)]


def test_a_dotdot_after_an_expansion_is_not_collapsed_into_the_directory_before_it() -> None:
    """`$D/..` is not where `$D` started when D holds two segments."""
    assert _patterns("echo x > $D/../acme_user.py") == [("${...}", "x\n", True)]


@pytest.mark.parametrize(
    "pattern, glob, suffix, expected",
    [
        ("\ue004/acme_user.py", "**/domain/**/*.py", "", True),
        ("src/d\ue006main/x.py", "**/domain/**/*.py", "", True),
        ("src/\ue005/x.py", "**/domain/**/*.py", "", True),
        ("build/\ue005.txt", "**/domain/**/*.py", "", False),
        ("build/x\ue004.py", "**/domain/**/*.py", "", True),
        ("src/app/\ue005.py", "**/domain/**/*.py", "", False),
        ("\ue004", "**/domain/**/*.py", ".py", True),
        ("\ue004", "**/domain/**/*.py", ".java", False),
        ("\ue004.md", "**/domain/**", ".py", False),
        ("src/main/\ue004", "src/main/java/**/domain/**/*.java", ".java", True),
        ("/tmp/\ue005.py", "src/**/domain/**/*.py", "", False),
    ],
)
def test_whether_a_shape_could_be_a_file_a_glob_covers(pattern: str, glob: str, suffix: str, expected: bool) -> None:
    assert pattern_matches_glob(pattern, glob, suffix) is expected


@pytest.mark.parametrize(
    "pattern, deletes, expected",
    [
        ("\ue004/settings.json", False, True),
        ("\ue004/.threefold.json", False, True),
        (".claude/\ue004", False, True),
        (".claude/setting\ue006.json", False, True),
        (".claude/\ue005", True, True),
        (".\ue005", True, True),
        ("\ue004/.git/hooks/pre-commit", False, True),
        ("build/\ue005", True, False),
        ("\ue005", True, False),
        ("\ue004", True, False),
        ("\ue004/report.txt", False, False),
        ("build/\ue004", False, False),
        (".claude/agents/\ue004.md", False, False),
        ("src/\ue004/x.py", False, False),
    ],
)
def test_whether_a_shape_could_be_one_of_the_files_that_run_the_hooks(pattern: str, deletes: bool, expected: bool) -> None:
    """The shell's `*` never matches a leading dot, so `rm -rf build/*` spares
    `.threefold.json`; and an expansion alone could be anything, so the literal
    part has to name what makes a path a governance path."""
    assert pattern_is_governance(pattern, deletes) is expected


def test_a_directory_tree_is_asked_about_the_files_at_the_root_only() -> None:
    assert pattern_is_governance("\ue004", tree=True) is True
    assert pattern_is_governance("src/\ue004", tree=True) is False
    assert pattern_is_governance(".claude/\ue004", tree=True) is True


# --- compound commands ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        f"if true; then cp /tmp/acme.py {DOMAIN}; fi",
        f"if false; then true; else cp /tmp/acme.py {DOMAIN}; fi",
        f"true && {{ cp /tmp/acme.py {DOMAIN}; }}",
        f"! cp /tmp/acme.py {DOMAIN}",
        f"while false; do cp /tmp/acme.py {DOMAIN}; done",
        f"case $x in a) cp /tmp/acme.py {DOMAIN};; esac",
        f"case $x in a) true;; b) cp /tmp/acme.py {DOMAIN};; esac",
        f"acme() {{ cp /tmp/acme.py {DOMAIN}; }}",
        f"function acme {{ cp /tmp/acme.py {DOMAIN}; }}",
    ],
)
def test_a_command_inside_a_compound_one_is_read(command: str) -> None:
    assert (DOMAIN, UNKNOWN) in _writes(command)


@pytest.mark.parametrize("command", [f"for i in 1; do echo 'import boto3'; done > {DOMAIN}", f"{{ echo 'import boto3'; }} > {DOMAIN}"])
def test_a_redirect_on_a_compound_command_carries_everything_it_printed(command: str) -> None:
    """Read as a redirect of nothing, this was an empty write and passed."""
    assert _writes(command) == [(DOMAIN, UNKNOWN)]


def test_removing_the_repository_config_inside_an_if_is_still_a_removal() -> None:
    deleted = [(w.target, w.deletes) for w in analyse("if true; then rm .threefold.json; fi").writes]
    assert deleted == [(".threefold.json", True)]


# --- escapes ----------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        f"echo -e 'import\\x20boto3' > {DOMAIN}",
        f"echo -e 'import\\0040boto3' > {DOMAIN}",
        f"printf 'import\\040boto3\\n' > {DOMAIN}",
        f"printf '\\151mport boto3\\n' > {DOMAIN}",
        f"printf '\\x69mport boto3\\n' > {DOMAIN}",
        f"printf '%b\\n' 'import\\x20boto3' > {DOMAIN}",
        f"printf '%c%s\\n' ixyz 'mport boto3' > {DOMAIN}",
        f"printf '%s%x%s\\n' 'import ' 11 'oto3' > {DOMAIN}",
    ],
)
def test_echo_and_printf_are_read_with_their_escapes_and_conversions_applied(command: str) -> None:
    assert [content.rstrip("\n") for _, content in _writes(command)] == ["import boto3"]


def test_echo_without_e_is_unknown_when_bash_and_zsh_would_print_different_things() -> None:
    assert _writes(f"echo 'import\\x20boto3' > {DOMAIN}") == [(DOMAIN, UNKNOWN)]
    assert _writes(f"echo -E 'a\\tb' > {DOMAIN}") == [(DOMAIN, UNKNOWN)]
    assert _writes(f"echo 'C:/acme' > {DOMAIN}") == [(DOMAIN, "C:/acme\n")]


def test_an_escape_nobody_can_be_sure_of_makes_the_content_unknown() -> None:
    assert _writes(f"printf 'import \\q' > {DOMAIN}") == [(DOMAIN, UNKNOWN)]
    assert _writes(f"printf '%q' boto3 > {DOMAIN}") == [(DOMAIN, UNKNOWN)]


@pytest.mark.parametrize(
    "script, added",
    [
        ("s/^/\\x69mport boto3/", "import boto3"),
        ("s/^/\\o151mport boto3/", "import boto3"),
        ("s/^/\\d105mport boto3/", "import boto3"),
        ("s/^/\\LIMPORT BOTO3/", "import boto3"),
        ("s/^/\\uimport boto3/", "Import boto3"),
        ("1i\\\nimport boto3", "import boto3"),
        ("1a\\\nfirst\\\nsecond", "first\nsecond"),
    ],
)
def test_sed_text_is_read_with_gnu_seds_escapes_applied(script: str, added: str) -> None:
    assert sed_added_text(script) == added


@pytest.mark.parametrize("script", ["s/boto/import &3/", "s/\\(b\\)/import \\13/", "s/^/\\q/"])
def test_sed_text_that_reuses_what_it_matched_is_unknown(script: str) -> None:
    assert sed_added_text(script) is UNKNOWN


def test_several_sed_expressions_are_joined_into_one_script_as_gnu_sed_joins_them() -> None:
    assert _writes(f"sed -i -e '1i\\' -e 'import boto3' {DOMAIN}") == [(DOMAIN, "import boto3")]


def test_a_sed_replacement_of_part_of_a_line_is_marked_as_a_fragment() -> None:
    [partial] = analyse(f"sed -i 's/json/boto3/' {DOMAIN}").writes
    [whole] = analyse(f"sed -i 's/^import json$/import boto3/' {DOMAIN}").writes
    assert (partial.content, partial.fragment) == ("boto3", True)
    assert (whole.content, whole.fragment) == ("import boto3", False)


def test_sed_w_writes_its_file_whether_or_not_in_place() -> None:
    assert _writes(f"sed -n 'w {DOMAIN}' /tmp/acme.py") == [(DOMAIN, UNKNOWN)]
    assert (DOMAIN, UNKNOWN) in _writes(f"sed 's/a/b/w {DOMAIN}' /tmp/acme.py")


def test_a_perl_replacement_is_read_as_the_double_quoted_string_it_is() -> None:
    assert _writes(f"perl -pi -e 's/^/\\x{{69}}mport boto3/' {DOMAIN}") == [(DOMAIN, "import boto3")]
    assert _writes(f"perl -pi -e 's/^/$x/' {DOMAIN}") == [(DOMAIN, UNKNOWN)]
    assert _writes(f"perl -pi -e 's/(b)/import \\13/' {DOMAIN}") == [(DOMAIN, UNKNOWN)]


# --- a backslash between letters ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "command, readings",
    [
        ("echo x > src/dom\\ain/acme_user.py", ["src/dom/ain/acme_user.py", DOMAIN]),
        ("echo x > src\\domain\\acme_user.py", [DOMAIN, "srcdomainacme_user.py"]),
        ("echo x > .claude/setting\\s.json", [".claude/setting/s.json", ".claude/settings.json"]),
    ],
)
def test_a_backslash_between_letters_is_read_both_as_a_separator_and_as_nothing(command: str, readings) -> None:
    assert [target for target, _ in _writes(command)] == readings


# --- redirects left open on the shell ------------------------------------------------------------------

@pytest.mark.parametrize("command", [f"exec 3> {DOMAIN}; echo 'import boto3' >&3", f"exec > {DOMAIN}; echo 'import boto3'", f"exec 3<> {DOMAIN}"])
def test_exec_with_only_a_redirect_leaves_the_file_open_for_whatever_follows(command: str) -> None:
    assert _writes(command) == [(DOMAIN, UNKNOWN)]


def test_a_read_write_redirect_on_standard_output_writes_what_was_printed() -> None:
    assert _writes(f"echo 'import boto3' 1<> {DOMAIN}") == [(DOMAIN, "import boto3\n")]


# --- the narrower routes --------------------------------------------------------------------------------

def test_patch_d_starts_every_path_in_its_directory() -> None:
    patch = "--- a/acme_user.py\n+++ b/acme_user.py\n@@ -0,0 +1 @@\n+import boto3\n"
    assert _writes(f"patch -p1 -d src/domain <<'EOF'\n{patch}EOF") == [(DOMAIN, "import boto3")]
    assert _writes(f"patch -p1 --directory=src/domain <<'EOF'\n{patch}EOF") == [(DOMAIN, "import boto3")]


def test_a_recursive_copy_from_outside_writes_anything_beneath_its_destination() -> None:
    assert [shape for shape in _patterns("cp -r /tmp/acme/. src/domain/") if shape[2]] == [("src/domain/${...}", UNKNOWN, True)]
    trees = [w for w in analyse("cp -r /tmp/acme/domain src/").writes if w.tree]
    assert [display(w.target) for w in trees] == ["src/domain/${...}"]


def test_a_recursive_copy_inside_the_project_is_asked_only_whether_its_destination_is_covered() -> None:
    trees = [display(w.target) for w in analyse("cp -r build/ dist/").writes if w.tree]
    assert trees == ["dist/build/*"]


def test_a_tree_copied_out_of_the_project_is_not_a_write_to_it() -> None:
    assert [w for w in analyse("cp -r src /tmp/acme-backup").writes if w.tree] == []


def test_a_tree_from_inside_the_project_to_a_place_nobody_can_name_is_not_recorded() -> None:
    """`cp -r src "$TMP/"` after mktemp: where it lands is unknowable and what it
    carries was already governed. A tree from outside is still recorded."""
    assert [w for w in analyse('cp -r src "$TMP/"').writes if w.tree] == []
    assert [display(w.target) for w in analyse('cp -r /tmp/acme "$TMP/"').writes if w.tree] == ["${...}/acme/${...}"]


def test_a_glob_copied_into_a_directory_is_a_shape_in_that_directory() -> None:
    assert _patterns("cp /tmp/acme/* src/domain/") == [("src/domain/*", UNKNOWN, True)]


def test_busybox_runs_its_applet() -> None:
    assert _writes(f"busybox cp /tmp/acme.py {DOMAIN}") == [(DOMAIN, UNKNOWN)]


def test_node_short_flags_run_together_still_carry_code() -> None:
    command = "node -pe \"require('fs').writeFileSync('src/domain/a.ts', 'x')\""
    assert _writes(command) == [("src/domain/a.ts", "x")]


def test_python_open_is_read_through_its_keyword_arguments() -> None:
    assert python_writes(f"open(file='{DOMAIN}', mode='w').write('import boto3')") == [(DOMAIN, "import boto3", "python open()", False)]


def test_python_names_imported_under_another_name_are_read_as_what_they_are() -> None:
    code = f"from pathlib import Path as P\nimport shutil as sh\nP('{DOMAIN}').write_text('import boto3')\nsh.copy('/tmp/a', 'src/domain/b.py')"
    found = python_writes(code)
    assert (DOMAIN, "import boto3", "python write_text()", False) in found
    assert ("src/domain/b.py", UNKNOWN, "python shutil.copy()", False) in found


def test_a_python_write_whose_path_cannot_be_worked_out_is_kept_with_no_path() -> None:
    writes = analyse("python -c \"import sys; open(sys.stdin.readline(), 'w').write('x')\"").writes
    assert [(w.target, w.route) for w in writes] == [(None, "python open()")]


def test_a_method_that_only_shares_a_name_with_a_file_operation_is_not_a_write() -> None:
    assert analyse("python -c \"import pandas as pd; df.rename(columns)\"").writes == []


def test_a_node_write_whose_path_is_not_a_literal_is_kept_with_no_path() -> None:
    assert [(w.target, w.route) for w in analyse("node -e \"require('fs').writeFileSync(p, 'x')\"").writes] == [(None, "node writeFileSync()")]


def test_xargs_and_find_exec_run_their_command_on_files_nobody_names() -> None:
    xargs = analyse("find src/domain -name '*.py' | xargs sed -i '1i import boto3'").writes
    found = analyse("find src/domain -name '*.py' -exec sed -i '1i import boto3' {} +").writes
    assert [(display(w.target), w.content) for w in xargs] == [("${...}", "import boto3")]
    assert [(display(w.target), w.content) for w in found] == [("src/domain/${...}", "import boto3")]


def test_the_code_of_a_program_named_by_a_variable_is_still_read() -> None:
    assert _writes(f"$PYTHON -c \"open('{DOMAIN}','w').write('import boto3')\"") == [(DOMAIN, "import boto3")]


# --- git: what it is told to skip, and what it removes ---------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null; git commit -m x",
        "GIT_CONFIG_KEY_0=core.hooksPath; git commit -m x",
        "GIT_CONFIG_GLOBAL=/tmp/acme.gitconfig git commit -m x",
        "git -c include.path=/tmp/acme.gitconfig commit -m x",
        "git -c $KEY=/dev/null commit -m x",
        "git config include.path /tmp/acme.gitconfig",
        "git config --unset core.hooksPath",
        "git config set core.hooksPath /dev/null",
    ],
)
def test_git_configuration_that_moves_the_hooks_is_found_wherever_it_is_set(command: str) -> None:
    assert analyse(command).tampering


@pytest.mark.parametrize(
    "command",
    ["git config core.hooksPath", "git config --show-origin core.hooksPath", "git config --local --show-scope core.hooksPath",
     "git config get core.hooksPath", 'git config user.email "$ACME_EMAIL"'],
)
def test_reading_the_hooks_setting_is_not_changing_it(command: str) -> None:
    assert analyse(command).tampering == []


def test_git_clean_x_removes_the_hooks_own_ignored_files() -> None:
    removed = [w.target for w in analyse("git clean -fdx").writes if w.deletes]
    assert ".threefold.json" in removed and ".claude/settings.local.json" in removed


@pytest.mark.parametrize("command", ["git clean -fd", "git clean -ndx", "git clean -fdx build/"])
def test_git_clean_that_spares_ignored_files_or_the_root_removes_none_of_them(command: str) -> None:
    assert [w for w in analyse(command).writes if w.deletes] == []


def test_git_clean_x_with_the_files_excluded_keeps_them() -> None:
    command = "git clean -fdx -e .threefold.json -e .claude -e .codex -e .agents"
    assert [w for w in analyse(command).writes if w.deletes] == []


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
    "a million unquoted stars": "echo " + "*" * 1_000_000 + " > src/domain/x.py",
    "unclosed brace groups": "echo " + "{a," * 200_000,
    "expansions in an unquoted heredoc": "cat > src/domain/x.py <<EOF\n" + "${" * 300_000 + "\nEOF",
    "backticks in an unquoted heredoc": "cat > src/domain/x.py <<EOF\n" + "`" * 500_000 + "\nEOF",
    "printf escapes": "printf '" + "\\x41" * 150_000 + "' > src/domain/x.py",
    "a target three hundred expansions deep": "cp /tmp/x " + "$a/" * 300 + "x.py",
    "a case arm on every word": "case x in " + "a) echo;; " * 50_000 + "esac",
    "xargs of xargs": "xargs " * 5_000,
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


def test_the_analysis_shared_between_gates_cannot_be_changed_by_one_of_them() -> None:
    """The boundary check, the observe pass and the loop gate share one cached
    analysis per command. A caller that appended to it would change what every
    later request in the same container is told about the same command."""
    from threefold.domain.boundary_guard import analysed

    shared = analysed(f"echo 'import boto3' > {DOMAIN}")
    with pytest.raises(AttributeError):
        shared.writes.append(None)  # type: ignore[attr-defined]
    assert analysed(f"echo 'import boto3' > {DOMAIN}") is shared


def test_a_long_command_that_cannot_write_is_not_marked_unread() -> None:
    assert analyse("echo " + "a" * 40_000).truncated is False
