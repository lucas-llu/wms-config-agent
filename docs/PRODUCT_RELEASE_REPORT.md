# Productization release candidate — Day 18

## Decision and scope

This is an offline, local productization candidate for Days 11–18, not production
approval. Day 17 was merged through PR #65. Day 18 remains on
`feature/product-release-gates` for user review; no deployment or release tag is created.

## Reproduce the evidence

```powershell
.\.venv\Scripts\python.exe scripts\run_product_release.py
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term --cov-fail-under=90
```

The release command always enforces its result through its exit code. It executes
fixed pytest suites in fresh temporary directories and summarizes the resulting
JUnit reports. It does not consume `agent_golden_results.json` or other prefilled
result files. That older report command remains a fixture/evaluation utility and
is not used as proof of execution by this gate.

The JSON report is written to `data/evaluation/product-release.json`. It contains
gate names, counts, status, Git revision, dirty-worktree flag and explicit unrun
acceptance categories. Test logs, exception bodies, JUnit case properties and
private source paths are not copied into it. Temporary raw JUnit output is removed
when each suite finishes. Missing/malformed output, zero tests, failures, errors,
skips, timeout and nonzero pytest exit all prevent a gate passing.

## Executed scenario matrix

| Gate | Coverage | Local result (2026-09-13) |
|---|---|---|
| V1 | Actual ingest-to-public-benchmark pytest scenario | 1 passed |
| Agent MVP | Existing six-scenario service-level golden test | 1 passed |
| Product | Workspace, catalog, diagnostics, feedback, workbench and new combinations | 55 passed |

All three gates passed with zero skips. Counts are pytest test cases, not claims
about the number of independently verified real-world WMS configurations.

Full local regression: **476 passed, 2 opt-in live-provider skips, coverage 91.05%**.
Ruff lint/format and dependency checks passed. Initial release execution used
commit `aa0a94e13573cccd9d1ce7193fabe90039818cb0` with a clean worktree; the final
generated JSON records its own execution revision. Documentation-only commits do
not change the tested runtime or gate code.

New combinations exercise actual application composition with fake provider/index
adapters (Agent enabled/disabled), live Actions/capability registry consistency,
feedback scope rejection, repeated reconstructed-v1-table migration followed by
feedback, preserved revision fingerprints, concurrent retry deduplication, restart
and unchanged approval/version state. The migration test reconstructs the old table
shape; it is not an execution of a historical release binary.

Ten runner tests verify fail-closed outcomes, stripped inherited pytest selection,
disabled live-provider flags and output privacy. The full repository suite tests
these runner guards separately; the product command does not recursively invoke itself.

## CI and rollback

The existing quality workflow retains lint, format, 90% source coverage, event/full
history/working-tree secret scans and V1 benchmark gates, then runs the new product
release command. Fix findings follow Issue #66 and internal bugfix PR #67.

This delivery changes testing/reporting and CI only; it adds no runtime schema
migration or WMS write capability. Revert the Day 18 PR to remove these gates while
retaining Day 17 runtime behavior. Before upgrading production-like stores, back up
the session database and validate on a copy; older-than-Day-12 binaries must not open
the v2 session schema. Feedback remains an additive table and must not be deleted as
part of an application rollback.

## Acceptance still required

- Authorized real-provider and representative private-corpus acceptance.
- Browser visual/accessibility review and customer workflow UAT.
- Production deployment, authentication/multi-tenancy and operational permission review.
- Any environment inspection or WMS mutation requires its separately scoped safety work.

Green offline gates do not remove these limitations or certify real WMS correctness.
