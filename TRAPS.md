# Threefold - Known Traps & Pitfalls

Append-only. Document anything that costs over 30 minutes to prevent re-learning.

## 2026-09-19: Initial Traps Seeded
- **Trap 1: Naive Token Cost Estimation vs Model Pricing Tiers.**
  Input tokens and output tokens have drastically different pricing (e.g. Claude 3.5 Sonnet: $3.00/1M input vs $15.00/1M output). The circuit breaker must compute cost using distinct input/output rate multipliers, not a single blended average, or cost overruns will trip late.
- **Trap 2: N-gram Loop Detection False Positives on Pagination.**
  Agents calling `list_dir` or pagination tools can trigger false loop positives if arguments aren't hashed alongside tool names. Loop detection must evaluate `hash(tool_name + canonical_json(args))` with an edit-distance threshold.
- **Trap 3: Preserving Sub-Millisecond Gate Latency.**
  The governance sidecar intercepts every agent tool call. If the evaluation gate exceeds 10ms, developer experience degrades. Keep all deterministic boundary checks in native Python standard library with pre-compiled regexes.
- **Trap 4: Never run local pip/npm installs.**
  Host environment has strict disk limits. All tests and runtime must rely on Python standard library and pre-installed pytest.

## 2026-10-01: Review batch

- **Trap 5: ENOSPC turns the suite into 800 errors.**
  With ~300 MB free, the full run ended 1 failed, 5647 passed, 815 errors across
  unrelated layers; every error reran green. At 0 bytes even the log cannot be
  written. Before a full run, check `Get-PSDrive C`; under 2 GB, purge `pip
  cache`, old `$env:TEMP/pytest-*` dirs and orphaned tool caches first.
- **Trap 6: A "linear" matcher is only linear per attempt.**
  The rm token walk read each token once per `rm` occurrence, so flags spelling
  `rm` (`--rm` x9000) restarted the walk per flag: quadratic, ~2 minutes. Share
  a memo of visited positions across attempts, and pin the adversarial input,
  not just the reported one.
- **Trap 7: Truncated storage blinds exact comparisons.**
  Bounding history arguments broke loop detection for large calls: the row held
  the bounded form, the judged call the whole, and the signatures never met.
  Compare through the same bound on both sides, in a module both the store and
  the gate can import.
- **Trap 8: Windows PowerShell 5.1 has no `SkipHttpErrorCheck`.**
  Use `urllib`/`HttpClient` for raw 4xx codes; `Invoke-WebRequest` throws on
  404/403 and the switch does not exist there.
