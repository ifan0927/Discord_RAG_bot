# Agent Loop Rule

This document defines how an agent moves from a user request to a mission draft, implementation, validation, repair, review, and report.

The goal is bounded autonomy. The agent may work independently inside a clear boundary, but must stop when decisions, scope, source of truth, or validation are no longer clear.

## Core Rule

The main conversation is the coordinator.

The coordinator may delegate bounded exploration, drafting, implementation, validation, or review work to subagents, but remains responsible for:

- Choosing the loop type.
- Preserving mission scope.
- Integrating subagent results.
- Detecting stop conditions.
- Asking the user for explicit approval.
- Approving transition from mission draft to implementation.

Subagents must not approve their own mission, expand scope, or silently resolve product, provider, model, DB, data lifecycle, or cross-module contract decisions.

## Loop Types

### Mission Draft Loop

Use when the user request is not yet an executable issue / mission.

Allowed:

- Read source of truth.
- Inspect the current project state.
- Identify gaps, conflicts, and open questions.
- Draft issue / mission fields.
- Ask convergence questions.
- Delegate bounded exploration or external research to subagents.

Not allowed:

- Modify implementation code.
- Treat a draft as an approved mission.
- Decide unconverged product or technical direction.
- Treat subagent recommendations as user approval.

Exit conditions:

- The user explicitly approves the issue / mission.
- The user asks to keep discussing, narrow scope, or rewrite the draft.
- Source-of-truth conflict or missing decisions require user convergence.

### Implementation Loop

Use only after an issue / mission satisfies `docs/agent/mission_issue_rule.md` and the user has explicitly approved it.

Allowed:

- Read source of truth listed by the mission.
- Split work into the smallest verifiable slices.
- Modify docs, code, and tests within scope.
- Run the smallest useful validation.
- Make focused repairs based on validation feedback.
- Report evidence.

Not allowed:

- Expand into Out of Scope.
- Reinterpret the mission goal.
- Decide unconverged provider, model, DB, deployment, or data lifecycle.
- Treat decisions that require the user as local implementation details.

### Review Loop

Use when there is an existing diff, PR, mission evidence, or implementation result to inspect.

Allowed:

- Check mission fit.
- Check scope.
- Check source-of-truth compliance.
- Check validation sufficiency.
- Delegate bounded fresh-context review to a subagent.

Not allowed:

- Expand into a new feature.
- Chase every nonessential suggestion.
- Treat reviewer suggestions as mandatory scope without triage.

## Source of Truth Check

Before starting any loop, the agent must identify which source-of-truth files apply.

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

If source-of-truth files conflict and the mission does not define priority, the agent must stop and report back.

## Coordinator And Subagent Use

Subagents are useful when:

- Multiple documents or code areas can be explored in parallel.
- External research or implementation practice summaries are needed.
- The task is large enough to split into independent slices.
- Fresh-context review is useful.
- The coordinator should avoid polluting the main conversation context with low-level details.

Subagents are not required for:

- Small single-file edits.
- Interactive product convergence with the user.
- Questions that only require reading one or two files.
- Work where a subagent result would directly cause an irreversible decision.

Mission Draft Loop may use an explorer subagent by default. Simple tasks may be handled directly by the coordinator.

Implementation Loop may use worker subagents, but each worker must have a clear and non-overlapping responsibility.

## Implementation Loop Steps

1. Intake
   - Read the issue / mission.
   - Confirm type, goal, in scope, out of scope, acceptance criteria, and stop conditions.
   - Confirm explicit user approval.

2. Source of Truth Check
   - Read the mission's source of truth.
   - Confirm there is no unresolved conflict.

3. Explore
   - Understand existing files, tests, and flow before editing.

4. Plan Slices
   - Split into the smallest verifiable slices.
   - Each slice should have a clear validation path.

5. Implement One Slice
   - Edit only what the slice requires.
   - Avoid speculative abstraction.

6. Validate
   - Run the smallest useful tests, build, compile, or manual check.
   - Feed validation results into the next step.

7. Repair
   - If validation fails, make focused repairs only.
   - Stop and report if the same failure does not converge after two repair rounds.

8. Internal Code Review
   - Required when the implementation loop creates any code diff.
   - A fresh-context subagent must review the code diff before the loop can be considered complete.
   - The coordinator must not approve its own code diff without this review.
   - The review must check mission scope, source-of-truth compliance, behavior bugs, test gaps, scope creep, and maintainability risks.
   - Review findings must be triaged as blocking or non-blocking.
   - Blocking findings must be fixed, validated, and sent through another internal code review round.
   - Non-blocking findings may be reported as remaining risk and do not block completion.
   - The implementation loop is complete only when validation passes and there are no blocking review findings.

9. Evidence Report
   - Report what was read, changed, validated, skipped, and what risk remains.

## Stop Conditions

### Hard Stop

The agent must stop and ask the user when:

- Source-of-truth files conflict.
- Mission scope is insufficient or does not match the current code state.
- Product, provider, model, DB, data lifecycle, or cross-module contract decisions are required.
- Real external services are required but the mission did not authorize them.
- The work must enter Out of Scope to complete the mission.
- Validation does not exist, or validation results contradict the mission goal.
- Destructive action, deployment, data deletion, or permission changes are required but not authorized.
- An internal code review finding requires entering Out of Scope, changing source-of-truth decisions, or deciding product, provider, model, DB, data lifecycle, or cross-module contracts.

### Soft Stop / Report

The agent may summarize evidence and ask whether to continue when:

- Local dependencies or test environment are missing.
- The same failure does not converge after two repair rounds.
- The diff is growing beyond the expected slice.
- An unrelated issue is discovered.
- Validation can only be partially executed.
- Blocking internal code review findings do not converge after two focused repair rounds.

## Evidence Format

Implementation Loop completion or stop report:

```md
## Evidence

- Mission:
- Source of Truth Read:
- Changes:
- Validation:
- Out of Scope Check:
- Remaining Risk:
```

Mission Draft Loop completion or stop report:

```md
## Draft Evidence

- Source of Truth Read:
- Current State:
- Proposed Mission:
- Open Questions:
- Stop Conditions:
```

Review Loop completion or stop report:

```md
## Review Evidence

- Reviewed Scope:
- Findings:
- Mission Fit:
- Validation Gaps:
- Recommendation:
```
