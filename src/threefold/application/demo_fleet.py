"""The synthetic Acme fleet: what the public demo's charts and tiles stand on.

Where the stack's DemoFleet parameter is true, an EventBridge Scheduler
schedule invokes the function every fifteen minutes with exactly
`{"threefold_fleet": {"tick": 1}}`, and `run_scheduled_tick` runs one tick: a
bounded batch of synthetic tool calls from Claude Code, Codex and Antigravity
across six Acme projects, each built the way a hook builds its request and sent
through the real evaluator (origin hook, explain false, hook_mode managed),
followed by what an operator of those projects would do: label some of the
calls a rule would have refused, promote a project whose rules are ready while
the noisy rule keeps observing, and now and then demote one.

What it is not:

- Not history. It writes only at the moment it runs, with the evaluator's own
  clock; nothing is backdated. The public demo's history is exactly as long as
  the fleet has been running, and the event carries no time a caller could set.
- Not a model caller. Every request says explain false, as a hook's does, and
  nothing here asks the reviewer for a sentence, so a tick never reaches
  Bedrock. It calls the evaluator directly rather than the HTTP route, so it
  emits none of the route's EMF metrics either: the call-volume alarm and the
  custom-metric bill measure callers of the API, and the fleet is not one.
- Not a route. `is_tick_event` accepts the one event the schedule sends and
  nothing an HTTP request can be turned into: an HTTP event always carries its
  request context, a path and headers beside anything its body says.

Deterministic per tick: everything a tick decides (which sessions work, what
each call is, which would-refuse calls are labelled now, whether an operator
acts) is drawn from a PRNG seeded by the tick's fifteen-minute bucket, so a
tick can be reproduced in a test from the same starting state. What the gates
answer still depends on that state, a project's stage and a session's history,
exactly as it would for a real fleet: a project in Observe records the layer
crossing that the same project in Enforce refuses, and an agent that was
refused corrects itself only because it was refused.

The mix is mostly ordinary work, reads, edits and test runs, approved. Beside
it: a domain module reaching for infrastructure, now and then a shell redirect
into a governed path, a credential shape, a protected path, and a build
repeated until the hook's loop rule stops it. One rule is deliberately noisy:
`python-domain-stays-pure` covers `**/domain/**/*.py`, which also matches a
test module under `tests/domain/` that drives the API with FastAPI's client.
That is test code, not the domain layer, so the fleet's operator marks those
calls false alarms, the rule reads noisy, and a promotion leaves it observing.

Every name is synthetic, as the clean-room rule requires. The credential
shapes are made up at run time from the PRNG, match nothing real, and never
appear in this file.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from threefold.application import ledger, rollups
from threefold.application import projects as stages
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.rule_keys import NONE, kind_of, rule_key, stored_rule_key, stored_rule_keys

logger = logging.getLogger("threefold.fleet")

FLEET_EVENT_KEY = "threefold_fleet"
TICK_SECONDS = 15 * 60
# A session spans four ticks, an hour, so the sessions page shows sessions of
# a realistic length rather than one per tick.
TICKS_PER_SESSION = 4
MIN_CALLS = 20
MAX_CALLS = 40
AGENTS = ("claude-code", "codex", "antigravity")
REVIEWER = "fleet"
SEED_SALT = "threefold-demo-fleet-v1"

# The operator. Two in five of this tick's would-refuse calls are labelled at
# once; the rest wait in the review queue for a sweep, which labels what is
# older than three hours, so the queue a visitor sees is small, real and moving.
LABEL_NOW_SHARE = 0.4
SWEEP_MIN_AGE = datetime.timedelta(hours=3)
SWEEP_HORIZON = datetime.timedelta(hours=8)
SWEEP_ROW_BUDGET = 1200
# Roughly half the six projects enforce at any time: promote while fewer than
# three do, demote about once a day, and let a demoted project observe for six
# hours before it can be promoted again.
TARGET_ENFORCE = 3
PROMOTE_CHANCE = 0.5
DEMOTE_CHANCE = 0.012
COOLDOWN = datetime.timedelta(hours=6)
# The window the project page reads readiness over, so the fleet promotes on
# the evidence a visitor sees, and how many calls a project must have had
# observed in it first: some hours of a project's work, not its first minutes.
READINESS_DAYS = 14
MIN_CALLS_OBSERVED = 120
# Stop sending calls this long before the function would time out. A timed-out
# asynchronous invocation is retried by Lambda, and a retried tick would send
# its batch again.
STOP_BEFORE_DEADLINE_SECONDS = 5.0
DEFAULT_BUDGET_SECONDS = 10.0

NOISY_RULE = "python-domain-stays-pure"
NOTES = {
    "correct": "Fleet operator: the rule caught what it is meant to catch.",
    "false_alarm": "Fleet operator: tests/domain is test code, not the domain layer; the rule's path is too wide.",
}


@dataclass(frozen=True)
class FleetProject:
    name: str
    suffix: str
    stack: str
    package: str
    modules: Tuple[str, ...]


PROJECTS: Tuple[FleetProject, ...] = (
    FleetProject("Acme-Payments", "payments", "python", "payments",
                 ("invoice", "refund", "settlement", "payout", "fee", "chargeback")),
    FleetProject("Acme-Checkout", "checkout", "web", "checkout",
                 ("cart", "basket", "promotion", "shipping", "tax", "voucher")),
    FleetProject("Acme-Ledger", "ledger", "java", "ledger",
                 ("account", "journal", "posting", "balance", "currency", "period")),
    FleetProject("Acme-Search", "search", "python", "search",
                 ("ranking", "query", "synonym", "facet", "snippet", "crawl_job")),
    FleetProject("Acme-Mobile", "mobile", "web", "mobile",
                 ("profile", "wallet", "notification", "onboarding", "preference", "device")),
    FleetProject("Acme-Platform", "platform", "dotnet", "Platform",
                 ("tenant", "quota", "subscription", "audit_event", "region", "plan")),
)
PROJECT_BY_NAME = {project.name: project for project in PROJECTS}
assert tuple(project.name for project in PROJECTS) == rollups.FLEET_PROJECTS


# ---------------------------------------------------------------- the event


def is_tick_event(event: Any) -> bool:
    """Whether a Lambda event is the schedule's tick, and nothing else.

    Exactly one key, `threefold_fleet`, holding exactly `{"tick": 1}`. An HTTP
    event from API Gateway, the edge or the local server always carries
    `requestContext` (with `http`), a path and headers, so it can never be this
    event whatever its body, query or headers say: a body arrives as a string
    under `body`, never as a key of the event.
    """
    if not isinstance(event, dict) or set(event) != {FLEET_EVENT_KEY}:
        return False
    if "requestContext" in event:  # implied by the line above; stated for the reader
        return False
    tick = event[FLEET_EVENT_KEY]
    return isinstance(tick, dict) and set(tick) == {"tick"} and type(tick["tick"]) is int and tick["tick"] == 1


def bucket_of(now: datetime.datetime) -> int:
    """The fifteen-minute bucket a moment falls in, counted from the epoch."""
    return int(now.timestamp()) // TICK_SECONDS


def _rng(bucket: int, stream: str) -> random.Random:
    digest = hashlib.sha256(f"{SEED_SALT}:{stream}:{bucket}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# ---------------------------------------------------------------- what a call is


@dataclass(frozen=True)
class Step:
    """One tool call an agent makes. `if_refused` calls answer the step before them."""

    tool: str
    action: str
    arguments: Mapping[str, Any]
    intent: str
    if_refused: bool = False


@dataclass(frozen=True)
class PlannedCall:
    session_id: str
    project: str
    agent: str
    developer: str
    step: Step

    def body(self) -> Dict[str, Any]:
        """The request body a hook would send for this call (request v2)."""
        return {
            "session_id": self.session_id,
            "project_name": self.project,
            "developer": self.developer,
            "tool_name": self.step.tool,
            "action_type": self.step.action,
            "arguments": json.loads(json.dumps(self.step.arguments)),
            "agent": self.agent,
            "origin": "hook",
            "explain": False,
            "dry_run": False,
            "hook_mode": "managed",
        }

    def request(self) -> ToolCallRequestDTO:
        return ToolCallRequestDTO.from_payload(self.body())


@dataclass(frozen=True)
class TickPlan:
    bucket: int
    target: int
    calls: Tuple[PlannedCall, ...]

    @property
    def fewest(self) -> int:
        """Calls sent if nothing is refused: every call that does not answer a refusal."""
        return sum(1 for call in self.calls if not call.step.if_refused)

    @property
    def most(self) -> int:
        return len(self.calls)


class _Tools:
    """An agent's own tool names, as its hook sends them."""

    def __init__(self, agent: str) -> None:
        self.agent = agent

    def read(self, path: str) -> Step:
        if self.agent == "codex":
            return Step("shell", "COMMAND_EXEC", {"command": f"cat {path}"}, "ordinary")
        if self.agent == "antigravity":
            return Step("view_file", "FILE_READ", {"file_path": path}, "ordinary")
        return Step("Read", "FILE_READ", {"file_path": path}, "ordinary")

    def write(self, path: str, content: str, intent: str = "ordinary", if_refused: bool = False) -> Step:
        tool = {"codex": "apply_patch", "antigravity": "write_to_file"}.get(self.agent, "Write")
        return Step(tool, "FILE_WRITE", {"file_path": path, "content": content}, intent, if_refused)

    def edit(self, path: str, old: str, new: str) -> Step:
        if self.agent == "claude-code":
            return Step("Edit", "FILE_WRITE", {"file_path": path, "old_string": old, "new_string": new}, "ordinary")
        tool = "apply_patch" if self.agent == "codex" else "replace_file_content"
        return Step(tool, "FILE_WRITE", {"file_path": path, "content": new}, "ordinary")

    def run(self, command: str, intent: str = "ordinary", if_refused: bool = False) -> Step:
        tool = {"codex": "shell", "antigravity": "run_command"}.get(self.agent, "Bash")
        return Step(tool, "COMMAND_EXEC", {"command": command}, intent, if_refused)


# ---------------------------------------------------------------- the four stacks


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _camel(name: str) -> str:
    pascal = _pascal(name)
    return pascal[0].lower() + pascal[1:]


@dataclass(frozen=True)
class _Module:
    """The files and commands of one module of one project, in its own language."""

    domain: str
    service: str
    test: str
    adapter: str
    config: str
    test_command: str
    build_command: str
    lint_command: str
    clean_domain: str
    corrected_domain: str
    forbidden: Tuple[Tuple[str, str], ...]
    adapter_body: str

    def crossing(self, line: str) -> str:
        """The domain module with one forbidden import at its top."""
        if self.clean_domain.startswith("package "):
            head, _, rest = self.clean_domain.partition("\n")
            return f"{head}\n\n{line}\n{rest}"
        return f"{line}\n\n{self.clean_domain}"


def _python(project: FleetProject, name: str) -> _Module:
    package, cls = f"acme_{project.package}", _pascal(name)
    clean = (
        "from dataclasses import dataclass\nfrom decimal import Decimal\n\n\n"
        f"@dataclass(frozen=True)\nclass {cls}:\n    id: str\n    amount: Decimal\n"
    )
    return _Module(
        domain=f"src/{package}/domain/{name}.py",
        service=f"src/{package}/services/{name}_service.py",
        test=f"tests/unit/test_{name}.py",
        adapter=f"src/{package}/infrastructure/{name}_store.py",
        config=f"src/{package}/settings.py",
        test_command=f"pytest -q tests/unit/test_{name}.py",
        build_command="python -m build --wheel",
        lint_command=f"ruff check src/{package}",
        clean_domain=clean,
        corrected_domain=(
            f"from {package}.domain.ports import {cls}Store\n" + clean
            + f"\n\ndef save(item: {cls}, store: {cls}Store) -> None:\n    store.save(item)\n"
        ),
        forbidden=(
            ("boto3", "import boto3"),
            ("requests", "import requests"),
            ("sqlalchemy", "from sqlalchemy import Column"),
            ("redis", "import redis"),
            ("httpx", "import httpx"),
        ),
        adapter_body=f"\nfrom {package}.domain.ports import {cls}Store\n\n\nclass Stored{cls}({cls}Store):\n    pass\n",
    )


def _java(project: FleetProject, name: str) -> _Module:
    cls, base = _pascal(name), f"com/acme/{project.package}"
    clean = (
        f"package com.acme.{project.package}.domain;\n\nimport java.math.BigDecimal;\n\n"
        f"public record {cls}(String id, BigDecimal amount) {{}}\n"
    )
    return _Module(
        domain=f"src/main/java/{base}/domain/{cls}.java",
        service=f"src/main/java/{base}/service/{cls}Service.java",
        test=f"src/test/java/{base}/{cls}ServiceTest.java",
        adapter=f"src/main/java/{base}/infrastructure/Jpa{cls}Repository.java",
        config="src/main/resources/application.properties",
        test_command=f"mvn -q test -Dtest={cls}ServiceTest",
        build_command="mvn -q package -DskipTests",
        lint_command="mvn -q spotless:check",
        clean_domain=clean,
        corrected_domain=clean + f"\ninterface {cls}Repository {{\n    void save({cls} item);\n}}\n",
        forbidden=(
            ("javax.persistence", "import javax.persistence.Entity;"),
            ("org.springframework", "import org.springframework.stereotype.Repository;"),
            ("java.sql", "import java.sql.Connection;"),
            ("org.hibernate", "import org.hibernate.Session;"),
            ("jakarta.persistence", "import jakarta.persistence.Table;"),
        ),
        adapter_body=f"\npublic class Jpa{cls}Repository {{}}\n",
    )


def _web(project: FleetProject, name: str) -> _Module:
    module, cls, root = _camel(name), _pascal(name), f"src/{project.package}"
    clean = (
        f"export interface {cls} {{\n  id: string;\n  amount: number;\n}}\n\n"
        f"export const total = (items: {cls}[]): number =>\n  items.reduce((sum, item) => sum + item.amount, 0);\n"
    )
    return _Module(
        domain=f"{root}/domain/{module}.ts",
        service=f"{root}/services/{module}Service.ts",
        test=f"{root}/__tests__/{module}.test.ts",
        adapter=f"{root}/infrastructure/{module}Http.ts",
        config=f"{root}/config.ts",
        test_command=f"npm test -- {module}",
        build_command="npm run build",
        lint_command="npm run lint",
        clean_domain=clean,
        corrected_domain=clean + f"\nexport interface {cls}Repository {{\n  save(item: {cls}): Promise<void>;\n}}\n",
        forbidden=(
            ("axios", "import axios from 'axios';"),
            ("react", "import { useState } from 'react';"),
            ("@aws-sdk/*", "import { DynamoDBClient } from '@aws-sdk/client-dynamodb';"),
            ("node-fetch", "import fetch from 'node-fetch';"),
            ("@prisma/*", "import { PrismaClient } from '@prisma/client';"),
        ),
        adapter_body=f"\nexport const {module}Http = {{}};\n",
    )


def _dotnet(project: FleetProject, name: str) -> _Module:
    cls, root = _pascal(name), f"src/Acme.{project.package}"
    clean = f"namespace Acme.{project.package}.Domain;\n\npublic sealed record {cls}(string Id, decimal Amount);\n"
    return _Module(
        domain=f"{root}/Domain/{cls}.cs",
        service=f"{root}/Services/{cls}Service.cs",
        test=f"tests/Acme.{project.package}.Tests/{cls}ServiceTests.cs",
        adapter=f"{root}/Infrastructure/Sql{cls}Repository.cs",
        config=f"{root}/appsettings.Development.json",
        test_command=f"dotnet test --filter {cls}ServiceTests",
        build_command="dotnet build -c Release",
        lint_command="dotnet format --verify-no-changes",
        clean_domain=clean,
        corrected_domain=clean + f"\npublic interface I{cls}Repository\n{{\n    void Save({cls} item);\n}}\n",
        forbidden=(
            ("System.Data", "using System.Data.SqlClient;"),
            ("Microsoft.EntityFrameworkCore", "using Microsoft.EntityFrameworkCore;"),
            ("System.Net.Http", "using System.Net.Http;"),
            ("Dapper", "using Dapper;"),
            ("Microsoft.AspNetCore", "using Microsoft.AspNetCore.Mvc;"),
        ),
        adapter_body=f"\npublic sealed class Sql{cls}Repository {{ }}\n",
    )


STACKS: Dict[str, Callable[[FleetProject, str], _Module]] = {
    "python": _python, "java": _java, "web": _web, "dotnet": _dotnet,
}


def _module(project: FleetProject, rng: random.Random) -> _Module:
    return STACKS[project.stack](project, rng.choice(project.modules))


# ---------------------------------------------------------------- episodes


def _secret_shape(rng: random.Random, prefix: str, alphabet: str, length: int) -> str:
    """A string of a credential's shape, made up here, never a credential."""
    return prefix + "".join(rng.choice(alphabet) for _ in range(length))


def _feature(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """Ordinary work: read, change a service, add a test, run it."""
    module = _module(project, rng)
    steps = [tools.read(module.service)]
    if rng.random() < 0.5:
        steps.append(tools.read(module.test))
    change = rng.choice(("rounding", "validation", "logging", "pagination", "retry", "naming"))
    steps.append(tools.edit(module.service, f"# TODO: {change}", f"# {change} handled below"))
    steps.append(tools.write(module.test, f"// covers the {change} change\n"))
    steps.append(tools.run(module.test_command))
    if rng.random() < 0.5:
        steps.append(tools.run(rng.choice(("git status", "git diff --stat", "git log --oneline -5"))))
    return steps


def _domain_edit(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """A domain change that stays inside the layer: approved in either stage."""
    module = _module(project, rng)
    return [
        tools.read(module.domain),
        tools.write(module.domain, module.corrected_domain),
        tools.run(module.test_command),
    ]


def _layering(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """A domain module reaching for infrastructure; refused, most agents correct it."""
    module = _module(project, rng)
    _, line = rng.choice(module.forbidden)
    steps = [tools.read(module.domain), tools.write(module.domain, module.crossing(line), "layering")]
    if rng.random() < 0.8:
        # Told what to do instead, the agent moves the client behind a port
        # and writes the adapter outside the domain, where the import is fine.
        steps.append(tools.write(module.domain, module.corrected_domain, "correction", if_refused=True))
        steps.append(tools.write(module.adapter, line + "\n" + module.adapter_body, "correction", if_refused=True))
    steps.append(tools.run(module.test_command))
    return steps


def _noisy(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """A test module under tests/domain/ that the Python rule's path also covers."""
    if project.stack != "python":
        return _feature(rng, project, tools)
    module = _module(project, rng)
    name = module.domain.rsplit("/", 1)[-1][: -len(".py")]
    package = f"acme_{project.package}"
    content = (
        "from fastapi.testclient import TestClient\n\n"
        f"from {package}.app import app\n\n\n"
        f"def test_{name}_totals_are_rounded():\n"
        f"    assert TestClient(app).get('/{name}s/1').status_code == 200\n"
    )
    return [
        tools.read(module.test),
        tools.write(f"tests/domain/test_{name}_api.py", content, "noisy"),
        tools.run(f"pytest -q tests/domain/test_{name}_api.py"),
    ]


def _shell_write(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """A write into a governed path through the shell rather than the write tool."""
    module = _module(project, rng)
    if rng.random() < 0.6:
        _, line = rng.choice(module.forbidden)
        command = f"cat >> {module.domain} <<'EOF'\n{line}\nEOF"
    else:
        command = f"cp build/generated/{module.domain.rsplit('/', 1)[-1]} {module.domain}"
    return [
        tools.read(module.domain),
        tools.run(command, "shell_write"),
        # The refusal says to use the write tool so the rule can read it.
        tools.write(module.domain, module.corrected_domain, "correction", if_refused=True),
        tools.run(module.test_command),
    ]


def _protected(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """A protected path: the environment file, a hook's own settings, a skipped hook."""
    module = _module(project, rng)
    settings = {"claude-code": ".claude/settings.json", "codex": ".codex/hooks.json"}.get(
        tools.agent, ".agents/hooks.json"
    )
    choice = rng.randrange(3)
    if choice == 0:
        attempt = tools.run(rng.choice(("cat .env", "cat .env.local")), "protected")
        instead = tools.read(".env.example")
    elif choice == 1:
        attempt = tools.write(settings, '{"hooks": {}}\n', "protected")
        instead = tools.read("README.md")
    else:
        attempt = tools.run(f"git commit --no-verify -m 'wip: {project.suffix}'", "protected")
        instead = tools.run(f"git commit -m 'feat({project.suffix}): small fix'")
    instead = Step(instead.tool, instead.action, instead.arguments, "correction", if_refused=True)
    return [tools.run(module.lint_command), attempt, instead]


def _loop(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """The same build three times running: the hook's loop rule stops the third."""
    module = _module(project, rng)
    config = {"python": "pyproject.toml", "java": "pom.xml", "web": "package.json"}.get(
        project.stack, f"src/Acme.{project.package}/Acme.{project.package}.csproj"
    )
    build = tools.run(module.build_command)
    third = Step(build.tool, build.action, build.arguments, "loop")
    return [build, build, third, tools.read(config), tools.run(module.lint_command)]


def _credential(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """A credential written into a file or a command; the agent then reads it from the environment."""
    module = _module(project, rng)
    if rng.random() < 0.5:
        key = _secret_shape(rng, "AKIA", string.ascii_uppercase + string.digits, 16)
        attempt = tools.write(module.config, f"AWS_ACCESS_KEY_ID = '{key}'\n", "credential")
    else:
        token = _secret_shape(rng, "ghp_", string.ascii_letters + string.digits, 36)
        attempt = tools.run(
            f"git remote set-url origin https://{token}@git.acme.test/acme/{project.suffix}.git", "credential"
        )
    instead = tools.write(module.config, "AWS_ACCESS_KEY_ID = env('AWS_ACCESS_KEY_ID')\n", "correction", if_refused=True)
    return [tools.read(module.config), attempt, instead]


# Out of 100 episodes. Ordinary work first; about one call in eight is one a
# rule flags, which is what a team adopting the rules sees in its first weeks.
EPISODES: Tuple[Tuple[str, Callable[..., List[Step]], int], ...] = (
    ("feature", _feature, 44),
    ("domain_edit", _domain_edit, 12),
    ("layering", _layering, 16),
    ("noisy", _noisy, 7),
    ("shell_write", _shell_write, 6),
    ("protected", _protected, 6),
    ("loop", _loop, 5),
    ("credential", _credential, 4),
)


def _filler(rng: random.Random, project: FleetProject, tools: _Tools) -> List[Step]:
    """One ordinary call, to top a batch up without passing its ceiling."""
    module = _module(project, rng)
    return [rng.choice((tools.read(module.service), tools.read(module.test), tools.run("git status")))]


# ---------------------------------------------------------------- the plan


def _session_id(project: FleetProject, agent: str, bucket: int) -> str:
    """fleet-<project suffix>-<agent>-<n>: one session an hour, each pair starting at its own quarter."""
    offset = int(hashlib.sha256(f"{project.name}:{agent}".encode("utf-8")).hexdigest(), 16) % TICKS_PER_SESSION
    return f"fleet-{project.suffix}-{agent}-{((bucket + offset) // TICKS_PER_SESSION) % 10000}"


def _developer(project: FleetProject, agent: str, session_id: str) -> str:
    """A 12-hex stand-in for a developer, as a hook computes one: two per project and agent."""
    seat = int(hashlib.sha256(session_id.encode("utf-8")).hexdigest(), 16) % 2
    return hashlib.sha256(f"{SEED_SALT}:{project.name}:{agent}:{seat}".encode("utf-8")).hexdigest()[:12]


def _target(bucket: int, rng: random.Random) -> int:
    """How many calls this tick aims for: more in working hours, fewer at night and at weekends."""
    moment = datetime.datetime.fromtimestamp(bucket * TICK_SECONDS, tz=datetime.timezone.utc)
    if 7 <= moment.hour < 18:
        activity = 1.0
    elif 18 <= moment.hour < 22:
        activity = 0.6
    else:
        activity = 0.3
    if moment.weekday() >= 5:
        activity *= 0.5
    wanted = MIN_CALLS + round((MAX_CALLS - MIN_CALLS) * activity * rng.uniform(0.75, 1.0))
    return max(MIN_CALLS, min(MAX_CALLS, wanted))


def _sessions(rng: random.Random) -> List[Tuple[FleetProject, str]]:
    """Four to six (project, agent) pairs working this tick, across at least three projects."""
    pairs = [(project, agent) for project in PROJECTS for agent in AGENTS]
    count = rng.randint(4, 6)
    while True:
        chosen = rng.sample(pairs, count)
        if len({project.name for project, _ in chosen}) >= 3:
            return chosen


def plan_tick(bucket: int) -> TickPlan:
    """Every call one tick will make, drawn from the bucket alone.

    `fewest` (the calls sent if nothing is refused) is at least MIN_CALLS and
    `most` (every correction sent too) at most MAX_CALLS, so a tick sends a
    number of calls between the two bounds whatever the gates answer.
    """
    rng = _rng(bucket, "plan")
    target = _target(bucket, rng)
    sessions = _sessions(rng)
    kinds = [kind for kind in EPISODES]
    weights = [weight for _, _, weight in EPISODES]
    calls: List[PlannedCall] = []
    fewest = 0
    turn = 0
    while fewest < target and len(calls) < MAX_CALLS:
        project, agent = sessions[turn % len(sessions)]
        turn += 1
        tools = _Tools(agent)
        _, build, _ = rng.choices(kinds, weights=weights)[0]
        steps = build(rng, project, tools)
        if len(calls) + len(steps) > MAX_CALLS:
            steps = _filler(rng, project, tools)
        session_id = _session_id(project, agent, bucket)
        developer = _developer(project, agent, session_id)
        for step in steps:
            calls.append(PlannedCall(session_id, project.name, agent, developer, step))
        fewest += sum(1 for step in steps if not step.if_refused)
    return TickPlan(bucket=bucket, target=target, calls=tuple(calls))


# ---------------------------------------------------------------- running it


@dataclass
class _Outcome:
    call: PlannedCall
    verdict_id: str
    timestamp: str
    status: str
    rule_key: str

    @property
    def refused(self) -> bool:
        return self.status != "APPROVED"

    @property
    def kind(self) -> str:
        if self.refused:
            return "refused"
        return "observed" if self.rule_key != NONE else "approved"

    def as_row(self) -> Dict[str, Any]:
        arguments = self.call.step.arguments
        return {
            "timestamp": self.timestamp,
            "verdict_id": self.verdict_id,
            "project_name": self.call.project,
            "session_id": self.call.session_id,
            "status": self.status,
            "rule_key": self.rule_key,
            "target": str(arguments.get("file_path") or ""),
        }


@dataclass
class TickSummary:
    tick: int
    calls: int = 0
    planned: int = 0
    verdicts: Dict[str, int] = field(default_factory=lambda: {"approved": 0, "observed": 0, "refused": 0})
    labelled: Dict[str, int] = field(default_factory=lambda: {"correct": 0, "false_alarm": 0})
    actions: List[Dict[str, Any]] = field(default_factory=list)
    stages: Dict[str, str] = field(default_factory=dict)
    cut_short: bool = False
    # Projects given a false alarm this tick: the only ones whose enforcing
    # rules can have turned noisy since the last tick. Not reported.
    false_alarms_in: set = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tick": self.tick,
            "calls": self.calls,
            "planned": self.planned,
            "verdicts": dict(self.verdicts),
            "labelled": dict(self.labelled),
            "actions": [dict(action) for action in self.actions],
            "stages": dict(sorted(self.stages.items())),
            "cut_short": self.cut_short,
        }


def is_false_alarm(row: Mapping[str, Any]) -> bool:
    """The noisy rule's call on a test module: what the fleet's operator marks a false alarm."""
    if stored_rule_key(row) != NOISY_RULE:
        return False
    target = str(row.get("observed_target") or row.get("target") or "").replace("\\", "/")
    return target.startswith("tests/") or "/tests/" in target


def _instant(stamp: Any) -> Optional[datetime.datetime]:
    try:
        moment = datetime.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def run_tick(
    evaluator: Any,
    now: Optional[datetime.datetime] = None,
    stop_at: Optional[float] = None,
) -> Dict[str, Any]:
    """One tick: the calls, then the operator. Returns a small summary.

    `now` places the tick in its bucket. The evaluator stamps every call with
    its own clock, and the operator acts once the calls are made, so its clock
    is the later of `now` and the last call's stamp: a review is never dated
    before the call it reviews. `stop_at` is a time.monotonic() after which no
    further call is sent and the operator does nothing.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    bucket = bucket_of(now)
    plan = plan_tick(bucket)
    summary = TickSummary(tick=bucket, planned=plan.most)
    _ensure_configured(evaluator, now)
    outcomes = _send(evaluator, plan, summary, stop_at)
    stamps = [moment for moment in (_instant(outcome.timestamp) for outcome in outcomes) if moment is not None]
    operator_now = max([now] + stamps)
    if _out_of_time(stop_at):
        summary.cut_short = True
    else:
        _operate(evaluator, bucket, operator_now, outcomes, summary, stop_at)
    summary.stages = {name: stages.stage_of(evaluator.project_config(name)) for name in rollups.FLEET_PROJECTS}
    return summary.to_dict()


def _out_of_time(stop_at: Optional[float]) -> bool:
    return stop_at is not None and time.monotonic() >= stop_at


def _ensure_configured(evaluator: Any, now: datetime.datetime) -> None:
    """Each fleet project exists as a configured project, starting in Observe as every project does."""
    configs = evaluator.list_project_configs()
    for name in rollups.FLEET_PROJECTS:
        if name not in configs and evaluator.project_config(name, fresh=True) is None:
            evaluator.save_project_config(name, stages.new_config(now.isoformat(), stage=stages.OBSERVE))


def _key_of(result: Any) -> str:
    """The rule key the ledger recorded for a verdict: the gate that decided, as the evaluator reads it."""
    decided = getattr(result, "decided_key", None)
    if isinstance(decided, str) and decided:
        return decided
    return rule_key(
        {
            "status": result.status,
            "reason": result.reason,
            "observed_rules": getattr(result, "observed_rules", None),
            "observations": getattr(result, "observations", None),
        }
    )


def _send(evaluator: Any, plan: TickPlan, summary: TickSummary, stop_at: Optional[float]) -> List[_Outcome]:
    outcomes: List[_Outcome] = []
    refused_last: Dict[str, bool] = {}
    for call in plan.calls:
        if call.step.if_refused and not refused_last.get(call.session_id, False):
            continue
        if _out_of_time(stop_at):
            summary.cut_short = True
            break
        result = evaluator.evaluate_tool_call(call.request())
        outcome = _Outcome(call, str(result.verdict_id), str(result.timestamp), str(result.status), _key_of(result))
        if not call.step.if_refused:
            refused_last[call.session_id] = outcome.refused
        outcomes.append(outcome)
        summary.calls += 1
        summary.verdicts[outcome.kind] += 1
    return outcomes


# ---------------------------------------------------------------- the operator


def _label(evaluator: Any, row: Mapping[str, Any], label: str, now: datetime.datetime, summary: TickSummary) -> None:
    """Labels one call as the reviews route does: on the ledger row, and moved in its day's rollup.

    Conditional on the row belonging to its project, as the route's is, and
    signed `fleet`, so a reader can tell the fleet's reviews from a person's.
    """
    repo = evaluator.session_repo
    labeller, adjust = getattr(repo, "label_decision", None), getattr(repo, "adjust_rollup", None)
    if labeller is None:
        return
    timestamp, project = str(row.get("timestamp") or ""), str(row.get("project_name") or "")
    before = labeller(
        timestamp,
        str(row.get("verdict_id") or ""),
        project,
        label,
        note=NOTES[label],
        reviewed_by=REVIEWER,
        reviewed_at=now.isoformat(),
    )
    if before is None:
        return
    summary.labelled[label] += 1
    if label == "false_alarm":
        summary.false_alarms_in.add(project)
    deltas = rollups.review_deltas(kind_of(before), stored_rule_keys(before), before.get("review"), label)
    if adjust is not None and deltas:
        try:
            adjust(timestamp[:10], project, deltas)
        except Exception as exc:  # pragma: no cover - a rollup never fails the tick
            logger.warning("Could not count a fleet review into the rollup: %s", exc)


def label_for(row: Mapping[str, Any]) -> Optional[str]:
    """The label the fleet's operator gives a call, or None when it leaves the call alone.

    Every call a rule would have refused is labelled, correct unless it is the
    noisy rule's test module. A refusal is labelled only when it is that false
    alarm: a wrong refusal is the one an operator cannot let stand.
    """
    kind = kind_of(row)
    if kind == "observed":
        return "false_alarm" if is_false_alarm(row) else "correct"
    if kind == "refused" and is_false_alarm(row):
        return "false_alarm"
    return None


def _review_now(evaluator: Any, bucket: int, now: datetime.datetime, outcomes: Sequence[_Outcome], summary: TickSummary) -> None:
    """A share (LABEL_NOW_SHARE) of this tick's would-refuse calls, and every refusal that was a false alarm."""
    rng = _rng(bucket, "labels")
    for outcome in outcomes:
        row = outcome.as_row()
        draw = rng.random()
        label = label_for(row)
        if label is None or (outcome.kind == "observed" and draw >= LABEL_NOW_SHARE):
            continue
        _label(evaluator, row, label, now, summary)


def _sweep(
    evaluator: Any,
    now: datetime.datetime,
    summary: TickSummary,
    project: Optional[str] = None,
    min_age: datetime.timedelta = SWEEP_MIN_AGE,
    stop_at: Optional[float] = None,
) -> None:
    """Labels the fleet's unreviewed calls older than `min_age`, newest first.

    Reads the ledger back SWEEP_HORIZON from `now`, at most SWEEP_ROW_BUDGET
    rows, so its cost is bounded however busy the stack is.
    """
    reader = getattr(evaluator.session_repo, "read_decision_day", None)
    if reader is None:
        return
    newest, oldest = now - min_age, now - SWEEP_HORIZON
    day, read = now.date(), 0
    while day >= oldest.date() and read < SWEEP_ROW_BUDGET:
        after: Optional[str] = None
        while read < SWEEP_ROW_BUDGET:
            if _out_of_time(stop_at):
                return
            rows, after = reader(str(day), after, min(ledger.PAGE_SIZE, SWEEP_ROW_BUDGET - read))
            read += len(rows)
            for row in rows:
                moment = _instant(row.get("timestamp"))
                if moment is None:
                    continue
                if moment < oldest:
                    return
                if moment <= newest and _is_fleet_row(row, project) and not row.get("review"):
                    label = label_for(row)
                    if label is not None:
                        _label(evaluator, row, label, now, summary)
            if after is None:
                break
        day -= datetime.timedelta(days=1)


def _is_fleet_row(row: Mapping[str, Any], project: Optional[str]) -> bool:
    name = row.get("project_name")
    if name not in PROJECT_BY_NAME or (project is not None and name != project):
        return False
    return str(row.get("session_id") or "").startswith("fleet-")


def _operate(
    evaluator: Any,
    bucket: int,
    now: datetime.datetime,
    outcomes: Sequence[_Outcome],
    summary: TickSummary,
    stop_at: Optional[float],
) -> None:
    _review_now(evaluator, bucket, now, outcomes, summary)
    _sweep(evaluator, now, summary, stop_at=stop_at)
    configs = evaluator.list_project_configs()
    fleet = {name: configs.get(name) for name in rollups.FLEET_PROJECTS}
    # A rule that turned noisy while enforcing goes back to observing first,
    # so the promotion below reads the stages as they now are.
    for name in sorted(summary.false_alarms_in):
        if stages.stage_of(fleet.get(name)) == stages.ENFORCE and not _out_of_time(stop_at):
            _record(summary, _observe_noisy(evaluator, name, now))
    for action in stage_actions(bucket, now, fleet):
        if _out_of_time(stop_at):
            return
        if action["action"] == "promote":
            _record(summary, _promote(evaluator, action["project"], now, summary, stop_at))
        else:
            _record(summary, _demote(evaluator, action["project"], now))


def _record(summary: TickSummary, action: Optional[Dict[str, Any]]) -> None:
    if action is not None:
        summary.actions.append(action)


def stage_actions(
    bucket: int, now: datetime.datetime, configs: Mapping[str, Optional[Mapping[str, Any]]]
) -> List[Dict[str, str]]:
    """What the operator means to do to the stages this tick: at most one promotion or demotion.

    Pure, so the policy can be read and tested apart from the store. Promote
    while fewer than TARGET_ENFORCE projects enforce; demote one about once a
    day when that many do (and at once when more do, which only a person
    promoting by hand can cause); a project demoted less than COOLDOWN ago is
    not promoted again, and one promoted less than COOLDOWN ago is not demoted.
    Whether a promotion happens is decided later, by readiness: a project with
    no rule Ready is left observing.
    """
    rng = _rng(bucket, "operator")
    enforcing = sorted(name for name, config in configs.items() if stages.stage_of(config) == stages.ENFORCE)
    observing = sorted(name for name in configs if name not in enforcing)
    promote_draw, demote_draw, pick = rng.random(), rng.random(), rng.random()
    if len(enforcing) < TARGET_ENFORCE:
        candidates = [name for name in observing if not _changed_within(configs.get(name), "demoted_at", now)]
        if candidates and promote_draw < PROMOTE_CHANCE:
            return [{"action": "promote", "project": candidates[int(pick * len(candidates))]}]
        return []
    if len(enforcing) > TARGET_ENFORCE or demote_draw < DEMOTE_CHANCE:
        settled = [name for name in enforcing if not _changed_within(configs.get(name), "promoted_at", now)]
        if settled:
            return [{"action": "demote", "project": settled[int(pick * len(settled))]}]
    return []


def _changed_within(config: Optional[Mapping[str, Any]], field_name: str, now: datetime.datetime) -> bool:
    """Whether a project's stage changed (by `field_name`) less than COOLDOWN before `now`."""
    moment = _instant((config or {}).get(field_name)) if (config or {}).get(field_name) else None
    return moment is not None and now - moment < COOLDOWN


def _keys(evaluator: Any, name: str) -> List[str]:
    rules, _ = evaluator.rules_in_force(name)
    return stages.project_rule_keys(rules)


def _readiness(evaluator: Any, name: str) -> Dict[str, Any]:
    """The project's readiness as its page shows it: same window, same rules, same function."""
    config = evaluator.project_config(name, fresh=True)
    rules, _ = evaluator.rules_in_force(name)
    items = evaluator.list_rollups(days=READINESS_DAYS, project=name)
    return rollups.readiness(items, config, rules)


def _readiness_rows(evaluator: Any, name: str) -> List[Dict[str, Any]]:
    return _readiness(evaluator, name)["rules"]


def _promote(
    evaluator: Any, name: str, now: datetime.datetime, summary: TickSummary, stop_at: Optional[float]
) -> Optional[Dict[str, Any]]:
    """Enforce the rules that are Ready or Quiet; every other rule keeps observing.

    Only a project with MIN_CALLS_OBSERVED calls observed in the readiness
    window is considered. The operator then reviews its queue, as the
    dashboard asks: a rule is Ready only when nothing it flagged is waiting.
    With no rule Ready after that, the project stays in Observe. The rules
    promoted are the ones the dashboard's promote dialog selects by default.
    """
    if _readiness(evaluator, name)["summary"]["calls_observed"] < MIN_CALLS_OBSERVED:
        return None
    _sweep(evaluator, now, summary, project=name, min_age=datetime.timedelta(0), stop_at=stop_at)
    rows = _readiness_rows(evaluator, name)
    if not any(row["state"] == "ready" for row in rows):
        return None
    keys = _keys(evaluator, name)
    enforce = [row["rule_key"] for row in rows if row["state"] in ("ready", "quiet")]
    config = evaluator.project_config(name, fresh=True)
    evaluator.save_project_config(name, stages.promoted(config, now.isoformat(), REVIEWER, enforce, keys))
    return {"action": "promote", "project": name, "enforce": enforce,
            "observe": [key for key in keys if key not in enforce]}


def _demote(evaluator: Any, name: str, now: datetime.datetime) -> Dict[str, Any]:
    config = evaluator.project_config(name, fresh=True)
    evaluator.save_project_config(name, stages.demoted(config, now.isoformat(), REVIEWER, _keys(evaluator, name)))
    return {"action": "demote", "project": name}


def _observe_noisy(evaluator: Any, name: str, now: datetime.datetime) -> Optional[Dict[str, Any]]:
    """An enforcing rule with a false alarm goes back to observing; the others keep enforcing."""
    rows = _readiness_rows(evaluator, name)
    noisy = [row["rule_key"] for row in rows if row["state"] == "noisy" and row["mode_now"] == stages.ENFORCE]
    if not noisy:
        return None
    enforce = [row["rule_key"] for row in rows if row["mode_now"] == stages.ENFORCE and row["rule_key"] not in noisy]
    config = evaluator.project_config(name, fresh=True)
    evaluator.save_project_config(name, stages.promoted(config, now.isoformat(), REVIEWER, enforce, _keys(evaluator, name)))
    return {"action": "observe_noisy", "project": name, "observe": noisy}


# ---------------------------------------------------------------- the schedule's entry point


def run_scheduled_tick(evaluator: Any, context: Any = None) -> Dict[str, Any]:
    """The function's answer to the schedule's event. Never raises.

    The schedule invokes the function asynchronously, and Lambda retries an
    asynchronous invocation that fails or times out, which would send a tick's
    batch twice. So the tick stops sending well before the deadline, and any
    failure is logged and answered rather than raised.
    """
    started = time.monotonic()
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    try:
        budget = max(0.0, remaining() / 1000.0 - STOP_BEFORE_DEADLINE_SECONDS) if callable(remaining) else DEFAULT_BUDGET_SECONDS
    except Exception:  # pragma: no cover - a context that cannot say is treated as the default
        budget = DEFAULT_BUDGET_SECONDS
    try:
        summary = run_tick(evaluator, stop_at=started + budget)
    except Exception:
        logger.exception("The demo fleet's tick failed; nothing is retried")
        return {FLEET_EVENT_KEY: {"ok": False}}
    summary["ok"] = True
    summary["seconds"] = round(time.monotonic() - started, 3)
    logger.info("Demo fleet tick: %s", json.dumps(summary, sort_keys=True))
    return {FLEET_EVENT_KEY: summary}
