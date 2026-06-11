# Review / Acceptance Rule

This document defines GitHub PR review and human acceptance expectations.

The goal is not to redesign the feature during review. The goal is to verify that the change faithfully completes the approved mission, stays in scope, follows source of truth, and leaves enough evidence to accept or request focused repair.

## Core Rule

Review must start from the approved issue / mission, not from the diff alone.

Reviewers should prioritize:

- Behavioral bugs.
- Scope expansion.
- Source-of-truth conflicts.
- Unconverged decisions silently implemented.
- Insufficient validation.
- Insufficient evidence.

Reviewers should not request:

- Features outside the mission.
- Unrelated refactors.
- Speculative abstractions.
- Pure preference style changes.
- Unconverged provider, model, DB, deployment, or data lifecycle decisions.

## GitHub PR Workflow

Review / acceptance is expected to happen through GitHub PRs.

Recommended flow:

```text
Approved Issue / Mission
-> Implementation Branch
-> Draft or Ready PR
-> Agent Evidence in PR Body or Comment
-> CI / Local Validation Evidence
-> CodeRabbit / External Review
-> Human Triage
-> Focused Repair if Needed
-> Human Acceptance
```

The PR must let reviewers answer:

- Which issue / mission does this PR implement?
- How were the acceptance criteria validated?
- Which source-of-truth files were read?
- Are any external reviewer findings unresolved?
- If validation was skipped, why?

The PR body or agent comment must include evidence, not only a change summary.

## Review Inputs

Before review, collect:

- Approved issue / mission.
- GitHub PR diff.
- Agent evidence report.
- Source-of-truth files listed by the mission.
- Actual validation results.
- CI results, if available.
- CodeRabbit or other external reviewer comments, if available.

If there is no approved mission, or approval cannot be confirmed, review should stop and ask for the missing contract. Do not turn review into scope definition.

## External Reviewer Rule

CodeRabbit or any other external reviewer is an assistant reviewer, not a scope owner.

External reviewer findings must be triaged as:

- `must fix`: real bug, security risk, data loss risk, mission violation, source-of-truth violation, or test failure.
- `should fix`: low-risk correctness issue, clear readability issue, or local issue likely to cause maintenance errors.
- `needs human decision`: product, scope, architecture, provider, model, DB, data lifecycle, or deployment decision.
- `out of scope / ignore`: feature outside the mission, preference refactor, unrelated suggestion, or suggestion that conflicts with source of truth.

The agent may handle `must fix` and clearly in-scope `should fix` findings.

The agent must stop and report `needs human decision` findings.

The agent must not expand PR scope just to satisfy an external reviewer.

If an external reviewer suggestion conflicts with the approved mission or source of truth, the approved mission and source of truth take priority. Record the triage conclusion in evidence.

## Review Checklist

### Mission Fit

Check:

- Does the change directly match the mission goal?
- Does every acceptance criterion have evidence?
- Was the negative acceptance criterion checked?
- Did implementation change the mission intent?

If the diff includes work the mission did not request, require removal unless it is necessary for the mission and still in scope.

### Scope Control

Check:

- Did the PR only touch in-scope files, modules, docs, or tests?
- Did it touch Out of Scope?
- Did it include opportunistic refactors, renames, moves, or doc restructuring?
- Did it add unrequested settings, CLIs, services, jobs, tables, or flows?

If completing the mission requires Out of Scope work, the review conclusion should be stop and ask, not "patch around it".

### Source of Truth

Check whether implementation follows the mission's source of truth.

Common project source of truth:

- `AGENTS.md`
- `docs/agent/mission_issue_rule.md`
- `docs/agent/agent_loop_rule.md`
- `docs/agent/review_acceptance_rule.md`
- `docs/agent/project_engineering_rules.md`
- `docs/design/bot_llm.md`
- `docs/design/runtime_flow.md`
- `docs/design/batch_pipeline.md`
- `docs/design/ddl.md`
- `docs/design/rag_schema.sql`

If source-of-truth files conflict, or if the diff needs source-of-truth changes to be valid, report the conflict and ask the user to converge it.

### Product And Architecture Boundaries

Check whether an unconverged decision was silently implemented.

This project should especially block:

- Implementing full RAG in one PR.
- Mixing runtime and batch work.
- Premature slash commands, dashboard, usage table, or prompt management.
- Treating JSONL as the long-term runtime source.
- Running migrations automatically on bot startup.
- Choosing LLM provider, model, DB, or Discord live integration without mission authorization.
- Reopening converged product scope.

### Validation

Check whether validation supports acceptance.

Reviewers should inspect actual commands and results, not accept "tested" as a claim.

Minimum checks:

- Bugfix PRs have a reproduction test or reasonable substitute validation.
- Implementation PRs have tests or compile/build checks directly related to the behavior.
- Docs-only PRs explain why tests were not run.
- Skipped validation has a concrete reason.
- Failed validation was fixed or listed as remaining risk.

Project validation commands are listed in `docs/agent/project_engineering_rules.md`.

### Evidence

PR or final report should include:

- Mission / issue reference.
- Source of truth read.
- Change summary.
- Validation commands and results.
- CI status, if available.
- External reviewer triage summary, if available.
- Out of scope check.
- Remaining risks or skipped validation.

If evidence is not enough to judge acceptance criteria, request evidence rather than a feature rewrite.

## Acceptance Rule

Human acceptance only needs to answer four questions:

1. Is the mission complete?
2. Did the PR stay in scope?
3. Does it follow source of truth?
4. Are evidence, CI, and external reviewer triage sufficient to accept?

All four must be yes before acceptance.

If any answer is no, return to the agent loop for focused repair, evidence, or a new mission / issue.

## Review Findings Format

Review findings should be findings-first and ordered by severity.

Each finding should include:

- File and line.
- Problem description.
- Why it violates the mission, source of truth, or acceptance criteria.
- Suggested repair direction.

Avoid findings that have no behavioral impact, no scope basis, or only reflect personal preference.

## Acceptance Evidence Format

Short acceptance format:

```md
## Acceptance

- Mission Fit:
- Scope Check:
- Source of Truth Check:
- Validation:
- External Review Triage:
- Remaining Risk:
- Decision:
```

`Decision` must be one of:

- `accepted`
- `needs focused repair`
- `blocked by scope / source-of-truth conflict`
- `needs new mission`
