---
name: autonomous-automation
description: Execute configurable automation tasks with audit, bounded retries, verification, and explicit handling of external side effects.
---

# Autonomous Automation

Use this skill when a task spans coding, GitHub, APIs, scheduled workflows, integrations, deployment, research, or other multi-step automation.

## Execution contract

1. Inspect the actual environment and available tools before acting.
2. Verify authentication and authorization rather than assuming access.
3. Read repository/project instructions before modifying files.
4. Decompose the request into independently testable phases.
5. Prefer small, reversible changes.
6. After each change, run the narrowest useful validation, then broader tests when appropriate.
7. Diagnose failures by root cause and retry safe fixes with a bounded retry count.
8. Never claim a connection, test, deployment, or task is complete without evidence.
9. Keep secrets out of source code, logs, diffs, and reports.
10. Continue independent work when one integration is blocked; record the blocker precisely.

## Default configuration

- MAX_RETRIES_PER_ISSUE: 3
- AUTO_COMMIT: true when the task explicitly authorizes repository changes
- AUTO_PUSH: true when repository write access is verified and the task explicitly authorizes it
- AUTO_MERGE: false
- AUTO_DEPLOY: false unless explicitly configured
- EXTERNAL_PUBLISHING: approval-controlled
- DESTRUCTIVE_ACTIONS: approval-controlled
- PAID_ACTIONS: approval-controlled

## Required workflow

AUDIT -> PLAN -> IMPLEMENT -> TEST -> VERIFY -> COMMIT -> REPORT

For failures:

REPRODUCE -> CLASSIFY -> FIX -> TEST -> VERIFY

Classify blockers as code, configuration, authentication, permission, quota, unavailable service, network, or external-policy related.

## GitHub safety

- Work from a dedicated branch for new changes unless an existing task branch is explicitly requested.
- Inspect the current branch and working state before rebasing or force-updating anything.
- Never force-push by default.
- Do not merge pull requests automatically unless explicitly configured.
- Do not modify unrelated changes.

## External services

Browser login is not proof of API authentication. Test the actual interface being used. Do not bypass CAPTCHA, MFA, platform restrictions, API quotas, or access controls.

## Reporting

At the end of each phase report:

- STATUS: PASS / PARTIAL / FAIL / BLOCKED
- WHAT CHANGED
- VALIDATION RUN
- VALIDATION RESULT
- REMAINING BLOCKERS
- NEXT STEP

Use evidence-based status only.
