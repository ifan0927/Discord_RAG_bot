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
- Run destructive git operations.

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
