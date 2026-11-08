# Harbor–ROSY boundary completion plan

Goal: implement ADR0013/design24 as enforced code boundaries, preserving every
existing execution, recovery, history and independent-evidence invariant. This is
the user-authorized completion task, not deployment or permission to push/merge.

## Completion criteria

- Fleet request/response fields, status normalization, digests and receipts belong
  to the integration adapter; business state transitions use normalized facts.
- User requests and the standing executor share one application service. The
  executor schedules work and handles retry timing, without wire/ledger policy.
- UI and application services use the ledger's read interface rather than SQL or
  serialized payload internals. Read-only access never recovers/creates a ledger.
- Architecture regression tests protect these ownership rules and allow explicit
  compatibility facades, configuration, backup/restore and developer harness owners.
- Existing full core/web suites, installed app probe and pinned real Fleet HTTP
  suites pass; skipped checks and physical/operational acceptance stay distinct.

## Constraints

No new service, database, workflow framework, public Fleet endpoint, approval bypass,
robot command owner, dependency or ledger migration. Keep original request keys,
mission/attempt/principal/digests/epoch/generation, evidence provenance and freshness.
Retain single-writer leasing, row-version guards, POST intent durability, original-key
readback after uncertainty, terminal history and read-only restored reconciliation.
The platform's internal Fleet app remains its D-822 composition root; external Harbor
continues to use public HTTP only. The verified process compilation branch is retained.

## 1. Normalize the execution port

Files: `harbor/execution.py`, new `harbor/fleet_contract.py`, `harbor/process.py`,
`harbor/transport.py`, `harbor/workflow.py`, relevant core and HTTP tests.

- [x] Move ARM/TRIP wire builders/validators to fleet_contract. Existing binding
  methods delegate there as compatibility facades, with no duplicate protocol logic.
- [x] ExecutionSnapshot exposes mission/request/attempt, Harbor phase and immutable
  normalized meaning, plus retained raw job/receipt audit facts. ARM, legacy Cell
  and TRIP adapter operations return that snapshot and reject unknown/mismatched facts.
- [x] BusinessLedger.submit returns a snapshot; propose remains a mission-ID
  compatibility wrapper. Business and transport ledgers retain durable transitions
  but no longer interpret Fleet status/response dictionaries.
- [x] Prove one POST plus original-key readback, shared snapshot identity, refusal
  of changed key/body/mission/principal/receipt, late/failed responses and no retry.

## 2. Publish the ledger read boundary

Files: new `harbor/ledger.py`, `harbor/workflow.py`, `harbor/web.py`,
`harbor_web/commands.py`, `harbor_web/evidence.py`, reader/restore tests.

- [x] Move read-only ledger selection, payload decoding and consumed-evidence lookup
  to the ledger owner. Share row reading/plan decoding with the writer.
- [x] Preserve ReadOnlyLedger import compatibility and existing display allowlists.
  Missing/invalid stores remain unavailable, never created or recovered by a read.
- [x] Application services and evidence intake use the repository interface without
  SQL or serialized plan parsing; backup/restore retain their explicit storage role.

## 3. Unify application execution

Files: `harbor_web/commands.py`, `harbor_web/executor.py`, application/executor tests.

- [x] Initial submit and subsequent stage submit use the same service implementation
  and validated submission snapshot; no additional unbound job read or principal check.
- [x] Move bounded business advancement into commands.advance_command. Executor.tick
  only selects eligible commands, invokes that service and manages loop/backoff.
- [x] Keep fresh clocks, queue/registry locks, pending cancellation exclusion, terminal
  history, independent step/final evidence and stored-identity checks intact.

## 4. Normalize presentation

Files: `harbor/web.py`, `harbor_web/pipeline.py`, `harbor/execution.py`, presentation tests.

- [x] Put live/saved Fleet classification in adapter projections. UI formats Harbor
  phases and retained source facts, without interpreting raw Fleet execution states.
- [x] Preserve separate receipt mismatch/unavailable reporting and strict refusal
  on state-changing paths; reading pages remains free of execution/ledger writes.

## 5. Enforce and verify

Files: core architecture/port tests, existing installed package test/tooling and
`docs/validation/` records, design24 and current boundary documentation.

- [x] Add boundary tests that would fail if business/executor/UI regained direct
  wire calls, SQL or protocol-state interpretation; test adapters through real HTTP.
- [x] Run `python -m unittest discover -s tests -v` and the full Django web suite.
- [x] Build/install the app in isolation and test representative business/adapters.
- [x] Run pinned Fleet ARM, authenticated web ARM, TRIP and legacy Cell suites on an
  approved test PC. Never use a skipped suite as proof of compatibility.
- [x] Obtain independent read-only review, fix supported findings, record exact SHAs,
  executed/skipped counts, limits and CE judgment, and leave a clean committed branch.

Each production step starts with a failing behavior/boundary test and retains
backward-compatible public methods where existing callers require them.

Completion: Harbor code `9b29c59465e245b9fb4aea602cbf3d7feb3cc85f` satisfies the
source boundary criteria. Full core/web, six pinned Fleet HTTP cases, isolated wheel
regression and independent review are recorded in
[validation](../../validation/2026-10-29-complete-application-boundaries.md).
Deployment, physical acceptance, main landing and push remain outside this goal.
