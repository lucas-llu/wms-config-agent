# Internal read-only canary preparation

Prepared 2026-09-14 after Day 18 PR #68 merged into dev. Updated 2026-09-27 after fresh
provider-stability revalidation.

## Current decision: provider stability green, still not ready to admit users

The configured model is now `deepseek-v4.1-flash`. The existing compatible gateway
and `WMS_LLM_API_KEY` variable name are unchanged. No key is stored in Git.
The user-designated CC Switch Codex / OpenCode Go credential was read from its
read-only database and temporarily injected into validation child processes. It was
not printed, committed, copied into a file, or persisted in Windows environment settings.
The application launcher must still securely supply the credential when started.

Live synthetic connectivity succeeded with reported model `deepseek-v4.1-flash`
(1.75 seconds). The missing `x-opencode-session` requirement was fixed via #71 / #72:
opaque task-local conversation IDs, stable across Agent turns, own User-Agent, and
host-specific header. See https://opencode.ai/docs/go/#where-can-i-use-it.

The 8192 output cap with thinking enabled now passes fresh live chunk-refinement/metadata,
intent and isolated synthetic multi-turn canary acceptance. Three consecutive multi-turn
canary rounds passed on 2026-09-27: first turns used 1,572-1,834 tokens, second turns used
5,037-7,153 tokens, each produced two tasks and 18 retrieval calls, and each paused at
`validation_blocked` as designed. No round hit timeout, token budget or retry budget.

The earlier 1024/4096 failures remain historical evidence for Issue #73 and draft PR #74.
No assertion was weakened. Offline regression on the current workbench candidate:
494 passed, 3 opt-in live-provider skips, coverage 91.04%.

Provider stability is no longer the blocker for synthetic canary entry. Canary admission
still requires the organization, data and operational inputs below; green synthetic tests
do not authorize private-corpus or user admission.

Set the credential securely in the process that launches the application, then
restart it if necessary. The application reads environment variables; merely placing
a key in a `.env` file does not establish that it will be loaded. Do not paste keys
into chats, test fixtures, PRs or logs.

## Proposed first canary scope (requires owner confirmation)

- A small named internal group, one isolated test Workspace and one approved module
  (inbound appointment/receiving).
- Authorized, version-matched documentation; synthetic or approved sanitized inputs.
- Generate and review configuration proposals only. No WMS writes or Environment Inspector.
- Business expert review before any manual operational use of a proposal.

## Entry checklist

1. Confirm gateway support for the exact model with a bounded synthetic request;
   record requested/reported model, success, latency and usage, never credentials.
2. Run the opt-in provider tests, then a complete multi-turn configuration scenario.
   A green intent-only test is not evidence that planning and review/export work.
3. Obtain data-owner approval before sending private excerpts to the existing external
   gateway. Curate representative success, ambiguity, missing evidence and version-conflict
   cases with expected outcomes agreed by a WMS expert.
4. Use a copy of the session store and a separate checkpoint/export directory. Validate
   migration and recovery on that copy; never use production data for an unapproved probe.
5. Review the workbench in a real browser: new/continue, historical revision, evidence,
   approval, export, feedback, disabled state and errors. AppTest is not visual UAT.
6. Restrict access at the host/network boundary. Workspace selection is not authentication.
   Do not expose the Dashboard to the internet; confirm allowlisted participants.
7. Agree request/token/cost limits, monitoring owner, feedback triage owner, entry/exit
   thresholds and stop authority before admitting users. Limits are not assumed approved.
8. Back up stores; rehearse stopping access and restoring the copy. Disabling Agent tools
   does not block existing read-only session views, so revoke access first when needed.

## Stop conditions

Immediately pause admission for cross-Workspace disclosure, unauthorized execution,
approval bypass, exposed secrets, or a confirmed unsafe configuration recommendation.
Pause for repeated provider failures or budget overruns according to the agreed limits.
Record an Issue, reproduce with sanitized data, fix on a bugfix branch and rerun gates
before resuming. Preserve audit evidence without logging private prompts or credentials.

## Remaining user inputs

- Secure application-launch credential injection (validation used temporary CC Switch loading).
- Named canary owner/users, approved corpus and business scenarios.
- Isolated host/environment and agreed operational limits.

No canary deployment, private-corpus upload, automatic regeneration or production
release is authorized by this preparation document.
