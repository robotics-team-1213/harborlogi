# Harbor execution reconciliation

Implement ADR0013 and design 24 for ARM reconciliation. Keep Fleet HTTP contracts,
business completion evidence, existing ledger schema and single-writer locking.

- [x] Add a regression showing that one web reconciliation reads one job and one
  physical receipt, and persists the same execution it applies to the ledger.
- [x] Put ARM response validation and normalized execution facts in
  `harbor/execution.py`, reusing `ArmTransferBinding` validation.
- [x] Give `BusinessLedger` one ARM reconciliation operation. Preserve the existing
  refresh/recover/resume entry points and their HOLD behavior.
- [x] Route web and executor reconciliation through `reconcile_command`, preserve
  original request/mission/attempt/digests/principal and reject changed identities.
- [x] Run core unittest and Django suites, including timeout recovery, cancellation,
  stale receipts, concurrent stage changes and independent completion evidence.

No new service, database, dependency, platform endpoint or automatic admission.
The existing HTTP client remains the transport boundary. Raw validated job and
receipt remain available for audit and read-only presentation.

## Verification — 2026-10-28

Base: upstream/main `ca2f207`; results apply to this worktree's uncommitted changes.

- Regression before implementation: FAILED, command UNKNOWN when a second job
  read was refused. After implementation: passes with one job and one receipt.
- `python -m unittest discover -s tests -v`: 172 run, OK, 1 skipped because real
  Fleet integration was not configured.
- `python -m harbor_web.manage test harbor_web.tests --noinput -v 1`: 233 run,
  OK, using `harbor_web.settings`, test mode and an in-memory web database.
- Read-only independent code review: no supported findings.
- Real Fleet contract suite, robot behavior and operational acceptance: not run.
- CE: 해당 없음 — the refactor and regression tests capture the reasoning; no
  separately verified device or operational recovery lesson was established.

2026-10-29 followup: committed the implementation, preserved the compatibility
entry points' original ledger version, and passed the pinned ARM and authenticated
web→Fleet HTTP suites on the model test PC. See
[the revision-bound verification record](../../validation/2026-10-29-execution-reconciliation.md).
