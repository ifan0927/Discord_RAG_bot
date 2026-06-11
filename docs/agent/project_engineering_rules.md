# Project Engineering Rules

This document defines project coding, testing, and Git flow constraints.

It complements `AGENTS.md` without making the root manifest too large.

## Coding Style

- Follow the existing simple Python style.
- Prefer the standard library and existing dependencies.
- Do not add an abstraction for one use.
- Do not add a framework, DB, provider, model, or deployment tooling unless the mission explicitly authorizes it.
- Do not mix runtime and batch flow in the same implementation slice.
- Do not treat JSONL as the long-term runtime source.
- Do not auto-run migrations on bot startup.
- Only clean up unused imports, variables, or orphan code caused by your own changes.
- Report unrelated existing dead code; do not remove it unless asked.

## Test Style

- Use the existing `unittest` style.
- Bugfix work should prefer the smallest test that reproduces the defect.
- Implementation work should add the smallest meaningful test directly related to the behavior.
- Docs-only work may skip tests, but must explain why in the report.
- Do not introduce large fixtures, mock frameworks, or new test tooling unless the mission explicitly authorizes it.

## Git Flow

This project uses a simplified Git flow:

- `prod`: production release branch. It may remain absent or non-default for now.
- `dev`: main integration branch and default PR target for daily work.
- `feature/...`: one mission / issue per feature branch, created from `dev` and merged back to `dev` through PR.

Before implementation, the agent should check the current branch.

Do not work directly on `prod` by default.

Unless explicitly requested by the user, the agent must not:

- Merge a feature branch directly into `prod`.
- Merge changes into `dev` before review / acceptance is complete.
- Reuse one feature branch for multiple unrelated missions.
- Run destructive git operations outside the post-PR cleanup workflow below.

## Default Completion Workflow

After completing a docs, implementation, bugfix, or ops / validation task, the agent should publish the result without waiting for a separate "commit / push / PR" prompt when all of the following are true:

- The current branch is a feature branch, not `dev`, `prod`, `main`, or `master`.
- The working tree contains only changes that belong to the current task.
- The smallest useful validation has passed, or the validation blocker is explicit and acceptable to report.

Default completion steps:

1. Run `git status --short --branch` and inspect the diff.
2. Run the smallest useful validation; docs-only work must at least run `git diff --check`.
3. Stage only files that belong to the current task.
4. Create a clear, terse commit.
5. Push the current branch; if it has no upstream, push with upstream tracking.
6. Open a GitHub PR targeting `dev`; PRs should be ready for review by default, not draft, unless the user explicitly asks for a draft PR.
7. Write a real PR body with change summary, rationale, validation evidence, skipped validation rationale if any, and remaining risks.
8. Report the commit SHA, branch, PR URL, validation, and any skipped checks.

Stop and ask before publishing when:

- The current branch is `dev`, `prod`, `main`, or `master`.
- The working tree includes unrelated changes.
- Validation fails in a way that is not an environment/tooling blocker.
- The task requires destructive Git operations outside post-PR cleanup, default-branch changes, deploys, data deletion, or merges.

The agent must not automatically merge PRs, change repository default branches, or run destructive Git commands outside the post-PR cleanup workflow unless the user explicitly asks for that operation.

## Post-PR Cleanup Workflow

After a PR is merged into `dev`, or after the user confirms that the PR is finished, the agent should perform standard Git flow cleanup without waiting for a separate prompt when all of the following are true:

- The current worktree is clean.
- The PR head branch is a feature branch.
- The PR was merged into `dev`, or the user explicitly says to clean up a closed / finished PR branch.

Default cleanup steps:

1. Fetch and prune remote refs.
2. Check out local `dev`.
3. Pull the latest `dev` from `origin` using fast-forward only.
4. Delete the local feature branch only after it is merged or explicitly safe to remove.
5. If the remote feature branch still exists and the PR was merged, delete the remote feature branch as part of cleanup.
6. Report the final branch, latest `dev` commit, deleted branches, and any cleanup skipped.

Stop and ask before cleanup when:

- The working tree is dirty.
- The PR is still open or unmerged.
- The branch contains commits not merged into `dev`.
- The branch is `dev`, `prod`, `main`, `master`, or another protected branch.
- Fast-forward pull of `dev` fails.

## Validation Commands

Current minimal local validation:

```bash
./.venv/bin/python -m unittest discover -s tests -p 'test*.py'
./.venv/bin/python -m compileall src tests
```

If `.venv` is missing or dependencies are not installed, report the actual blocker. Do not claim validation was completed.

## Documentation Sync

An implementation issue may include small documentation updates directly related to the behavior being changed.

Do not opportunistically:

- Redesign runtime / batch flow.
- Decide provider, model, DB, or deployment approach.
- Document Out of Scope features as future commitments.
- Heavily restructure docs.
