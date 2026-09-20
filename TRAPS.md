# Threefold — Known Traps & Pitfalls

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
