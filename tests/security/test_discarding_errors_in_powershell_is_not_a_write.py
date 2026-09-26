"""A PowerShell read that throws its errors away is not refused, and `bash -c "...\\$f"` is read as bash runs it.

The first daily real-agent run on the public stack, on 2026-09-26, had Codex
on Windows refused for `Get-Content README.md; rg --files -g AGENTS.md
C:/threefold-bench 2>$null`, a read: `2>$null` is PowerShell's `2>/dev/null`,
and the command check read it as a write to a shell expansion under a rule
that could cover it. In bash `$null` is an ordinary variable, so it is read as
the null device only when nothing in the command could have given it a value.

Looking for a way around that found an older hole: inside double quotes the
shell drops the backslash before `$`, so `bash -c "echo ... > src/domain/\\$f"`
hands the inner shell `$f` to expand, and the check had read it as a file
literally named `$f`. Both halves are pinned here, through the same route a
hook's call takes.
"""
from __future__ import annotations

import json

import pytest

from threefold.domain import shell_writes
from threefold.interfaces.api_handlers import lambda_handler

READS = [
    "Get-Content README.md; rg --files -g AGENTS.md C:/threefold-bench 2>$null",
    "rg foo 2>$null",
    "rg foo 2> $null",
    "rg foo >$null",
    "rg foo >>$null",
    "rg foo *>$null",
    "rg foo 2>${null}",
    "rg foo 2>$NULL",
    "Get-ChildItem -Recurse -Filter *.cs 2>$null | Select-Object -First 5",
    "rg x 2>$null | Out-Null",
    "pwsh -Command 'rg x 2>$null'",
    "Get-ChildItem -File | ForEach-Object { $_.FullName } 2>$null",
]

STILL_REFUSED = [
    # Something in the command could give `null` a value, so `$null` stays an expansion.
    "null=src/domain/user.py; echo 'import boto3' > $null",
    "export null=src/domain/user.py; bash -c \"echo 'import boto3' > \\$null\"",
    "eval \"nu\"\"ll=src/domain/user.py\"; echo 'import boto3' > $null",
    "\\eval nu''ll=src/domain/user.py; echo 'import boto3' > $null",
    "e$'v'al n$'u'll=src/domain/user.py; echo 'import boto3' > $null",
    "x=ev; $x''al n=1; echo 'import boto3' > $null",
    "read null < names.txt; echo 'import boto3' > $null",
    "mapfile -t null < names.txt; echo 'import boto3' > $null",
    "printf -v nu''ll src/domain/user.py; echo 'import boto3' > $null",
    "declare \"$(printf n%sll u)=src/domain/user.py\"; echo 'import boto3' > $null",
    ": ${null:=src/domain/user.py}; echo 'import boto3' > $null",
    "for null in src/domain/user.py; do echo 'import boto3' > $null; done",
    ". ./env.sh; echo 'import boto3' > $null",
    "sourc? env.sh; echo 'import boto3' > $null",
    # Ways bash gives a name a value it is handed. On 2026-09-26 the two traps,
    # the indirect default, BASH_ENV, --rcfile and an alias (with
    # expand_aliases on) each did so in a real bash; ENV and a login shell are
    # refused because they read a file first, whether or not this one sets it.
    'x=u; trap "n${x}ll=src/domain/user.py" DEBUG; echo "import boto3" > $null',
    "trap 'eval nu\"\"ll=src/domain/user.py' DEBUG; echo 'import boto3' > $null",
    'x=u; y=n${x}ll; : ${!y:=src/domain/user.py}; echo "import boto3" > $null',
    "BASH_ENV=./env.sh bash -c 'echo import boto3 > $null'",
    "ENV=./env.sh sh -c 'echo import boto3 > $null'",
    "bash --rcfile ./env.sh -ic 'echo import boto3 > $null'",
    "bash -lc 'echo import boto3 > $null'",
    "alias x='eval nu\"\"ll=src/domain/user.py'; echo 'import boto3' > $null",
    # Not `$null` at all.
    "echo 'import boto3' > $null.py",
    "echo 'import boto3' > $nullx",
    "echo 'import boto3' > $null/user.py",
    # The inner shell expands what the outer one's double quotes hand it.
    "export x=src/domain/user.py; bash -c \"echo 'import boto3' > \\$x\"",
    "bash -c \"echo 'import boto3' > src/domain/\\$f\"",
    "echo 'import boto3' > src/domain/user.py",
]


def _verdict(command: str, index: int) -> dict:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.9.5.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": f"powershell-null-{index}-{abs(hash(command))}",
            "project_name": "Acme-Ledger",
            "tool_name": "Bash",
            "action_type": "COMMAND_EXEC",
            "arguments": {"command": command},
            "agent": "codex",
            "origin": "hook",
            "explain": False,
        }),
    }
    return json.loads(lambda_handler(event, None)["body"])


@pytest.mark.parametrize("index, command", list(enumerate(READS)))
def test_a_read_that_discards_its_errors_is_approved(index: int, command: str) -> None:
    assert shell_writes.analyse(command).writes == [], command
    verdict = _verdict(command, index)
    assert verdict["status"] == "APPROVED", verdict.get("reason")


@pytest.mark.parametrize("index, command", list(enumerate(STILL_REFUSED, start=100)))
def test_a_write_that_could_land_in_the_domain_is_still_refused(index: int, command: str) -> None:
    verdict = _verdict(command, index)
    assert verdict["status"].startswith("BLOCKED"), command


def test_an_escaped_dollar_in_double_quotes_is_a_dollar() -> None:
    """`"\\$HOME"` is the five characters `$HOME`: readable content, not an expansion and not a backslash."""
    writes = shell_writes.analyse('echo "\\$HOME" > notes.txt').writes
    assert [(write.target, write.content) for write in writes] == [("notes.txt", "$HOME\n")]


def test_a_nested_script_sees_the_expansion_the_outer_quotes_left_it() -> None:
    writes = shell_writes.analyse("bash -c \"echo 'import boto3' > src/domain/\\$f\"").writes
    assert len(writes) == 1 and writes[0].pattern, writes
    assert shell_writes.display(writes[0].target) == "src/domain/${...}"
