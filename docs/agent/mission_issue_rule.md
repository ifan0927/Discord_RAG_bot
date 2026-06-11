# Mission / Issue Rule

This document defines how to open an issue or mission so it can act as the external contract for an agent loop.

GitHub issues are the expected tracking unit for work, discussion, PR linkage, and acceptance. An agent prompt may reference an issue, but should not replace the issue itself.

The goal is not to make every issue long. The goal is to make context, goal, boundaries, source of truth, acceptance criteria, and stop conditions explicit enough that an agent can work within the boundary and stop when the boundary is no longer sufficient.

## Core Rule

Every issue / mission must answer four questions:

1. Why does this work exist?
2. What state counts as done?
3. What is explicitly not allowed?
4. When must the agent stop and report back?

A GitHub issue must also let reviewers understand:

- Why the work was opened.
- Which documents or previous decisions apply.
- Whether a PR truly matches the issue.
- Which acceptance criteria were completed and which were not.

## Issue Types

Use only these issue types:

- `docs / convergence`: clarify decisions, update docs, or close gaps.
- `implementation`: implement according to existing source of truth.
- `bugfix`: fix a clear defect or regression.
- `ops / validation`: deployment, validation, import, or one-off operation.

Different issue types allow different work. An `implementation` issue must not reopen product scope. A `docs / convergence` issue may discuss tradeoffs and update docs, but should not also land code unless explicitly authorized.

## Required Fields

### Type

Must be one of:

```md
docs / convergence | implementation | bugfix | ops / validation
```

### Context

Describe the current state, gap, or problem. Do not smuggle in an unresolved implementation choice.

Good context explains the problem and constraints. Bad context silently mandates an unconverged architecture, abstraction, provider, or model.

### Goal

Describe the successful end state in one sentence.

The goal should describe an outcome, not an implementation method.

### Source of Truth

List the documents, schema, code, or external contracts this issue must follow.

Common project source of truth:

- `AGENTS.md`: top-level agent behavior rules.
- `docs/agent/mission_issue_rule.md`: issue / mission contract.
- `docs/agent/agent_loop_rule.md`: agent loop execution rules.
- `docs/agent/review_acceptance_rule.md`: PR review and acceptance rules.
- `docs/agent/project_engineering_rules.md`: coding, testing, and Git flow rules.
- `docs/design/bot_llm.md`: product scope and exclusions.
- `docs/design/runtime_flow.md`: Discord mention runtime flow.
- `docs/design/batch_pipeline.md`: import, batch, and backfill flow.
- `docs/design/ddl.md` / `docs/design/rag_schema.sql`: RAG schema and field semantics.

If source-of-truth documents conflict and the issue does not define priority, the agent must stop and report back.

### In Scope

List the behavior, documents, modules, or tests this work may touch.

In Scope should not be written as "finish the whole system".

### Out of Scope

Every issue must include Out of Scope.

List work that the agent must not do, especially nearby features that are easy to overbuild.

### Acceptance Criteria

Use verifiable conditions.

Every issue must include at least one negative acceptance criterion that confirms the agent did not exceed scope.

Good acceptance criteria can be checked through tests, commands, docs diffs, or clear manual steps. Bad acceptance criteria only say "clean architecture", "future-proof", or "complete support".

### Stop Conditions

List when the agent must stop and ask the user.

Default stop conditions:

- Source-of-truth documents conflict.
- The task requires an unconverged product or technical decision.
- The task requires choosing a provider, model, DB, data lifecycle, or cross-module contract.
- Real external services are required but the issue did not authorize them.
- The work must expand into Out of Scope to be completed.
- Validation does not exist, or validation results contradict the mission goal.
- The current code state does not match the issue assumptions.

## Optional Fields

### Source of Truth Priority

Use only when priority matters.

If priority is absent and source-of-truth documents conflict, the agent must stop and ask.

### Suggested Slices

Optional. Use only for larger work.

Suggested slices should describe delivery slices, not implementation micromanagement.

Good:

```md
1. Add config validation.
2. Add raw message persistence.
3. Add tests for ignored events.
```

Bad:

```md
1. Create class X.
2. Add method Y.
3. Use repository pattern Z.
```

### Validation Hints

Optional.

Known commands or manual checks may be listed here, but the agent is still responsible for choosing the smallest useful validation.

## Local Detail Rule

The agent may fill in local implementation details, but must not independently decide:

- Product boundaries.
- Architecture choices.
- Provider / model.
- DB or data lifecycle.
- Cross-module contracts.
- Excluded items already settled in source-of-truth docs.

If the task contains unconverged product, data, model, provider, deployment, or acceptance decisions, the first issue should be docs / convergence, not implementation.

## Docs And Code Rule

An `implementation` issue may include small documentation updates that are directly required by the code change.

Allowed:

- Sync README or design status after implementation.
- Add configuration notes directly related to this behavior.
- Fix docs made stale by this change.
- Add a short validation note or known limitation.

Not allowed:

- Redesign runtime / batch flow.
- Decide an unconverged model, provider, DB, or deployment approach.
- Document Out of Scope features as future commitments.
- Restructure docs heavily unless the issue itself is `docs / convergence`.

Must stop:

- The implementation requires modifying source of truth to be valid.
- Docs and code conflict in a way that cannot be fixed by a small sync.
- A doc gap represents an unconverged product or technical decision.
- A small doc change would affect another issue or rule.

## Template

```md
## Type

docs / convergence | implementation | bugfix | ops / validation

## Context

## Goal

## Source of Truth

## Source of Truth Priority

Optional. Only fill this when priority matters.

## In Scope

## Out of Scope

## Acceptance Criteria

Must include at least one negative acceptance criterion.

## Stop Conditions

## Suggested Slices

Optional.

## Validation Hints

Optional.
```
