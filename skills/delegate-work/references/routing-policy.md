# Routing and Review Policy

Optimize accepted work per Codex token. Claude subscription implementation is the default while the user retains that subscription. A quota window or login failure does not imply subscription cancellation. Changing this preference requires the user's instruction.

## Responsibilities

- Codex: requirements, bounded investigation and diagnosis, architecture, assignments, test strategy, review, acceptance, and authorized integration/release work.
- Claude: code and associated tests, bounded local exploration, focused checks and correction of implementation failures, and Codex review findings.
- Grok: the same implementation role after verified Claude five-hour exhaustion or explicit user selection.
- Native Codex helpers: optional read-only analysis/review when independence or parallelism provides a concrete benefit. Use minimum context and explicit suitable model/effort, not the full conversation.

The current lead normally plans and reviews. Do not spawn a planner because a task is large or a reviewer just to follow a sequence. Handle advice/read-only work directly in Codex. Batch small related implementation edits into a Claude assignment; direct Codex implementation requires the user's explicit preference for that task.

## Review depth

| Change | Codex preparation and acceptance |
| --- | --- |
| Simple, localized | Compact scope; inspect diff and focused check evidence. No separate reviewer. |
| Moderate, related files | Resolve interfaces; inspect affected call paths and regression evidence. Lead reviews by default. |
| Complex or ambiguous | Resolve material design questions first; split at independently verifiable boundaries. Review integration and assumptions. |
| High consequence | Identify concrete failure scenarios for security, data integrity, concurrency, migrations, or major architecture. Add one independent read-only Codex review when it addresses that risk. |

All writing assignments require Codex acceptance. Optional helpers should use Sol for ordinary review and Astra for difficult, high-consequence reasoning at the lowest sufficient effort. Preserve the user's chosen lead model/effort. At most two independent read-only helpers may run concurrently; justify duplicated context cost.

## Capacity and fallback

The Claude bridge owns quota retrieval, caching, retry, and launch/continuation gates. Use the [Claude procedure](claude-workflow.md) for exact commands. Use verified five-hour capacity down to exhaustion; 5% remaining is a warning, not a fallback trigger. Shorten future segments near exhaustion. Weekly readings remain advisory unless the provider actually blocks work.

- Verified zero/exhausted five-hour capacity or an unambiguous provider five-hour-limit response permits Grok fallback. Unknown usage, transient 429, login failure, timeout, generic errors, and review defects do not.
- Before switching, ensure Claude is no longer writing, inspect preserved work, and define only the remainder. Export a verified handoff and seed Grok's separate worktree through its adapter. Review the combined diff against the original base.
- If integrity cannot be verified, preserve evidence and diagnose. Never silently restart the entire task, discard partial changes, or commit the user's checkout to manufacture a base.
- Grok exposes session tokens and estimated API-equivalent cost, not reliable subscription-window headroom. Keep headroom unavailable and respect the user's stated Grok budget. Never switch to API-key billing.
- Check Claude again at the next assignment boundary. Prefer it after the reset, allowing a running Grok assignment to finish. Do not ping-pong providers during a segment.
- An actual weekly/account block is distinct from this five-hour fallback policy; report its cause instead of relabeling generic quota errors.

## Token discipline

1. Supply paths, decisions, criteria and relevant context. Avoid copying full files or history the worker can read. Permit bounded factual exploration needed for implementation.
2. Keep one canonical assignment. Send only changed requirements or concrete findings into the same session. Read concise checkpoints, not routine transcripts.
3. Claude normally gets 8-12 work turns for substantial work, fewer for small work/low capacity; Grok initially 3-6. Continue on progress without arbitrary task-wide turn ceilings.
4. Return a clear compiler/test failure directly to the worker with the relevant error and expected behavior; do not re-diagnose or pre-write the fix unless it reveals a design or scope issue. Let the worker fix implementation failures. Where worker shell execution is unavailable, the bridge runs the focused validation command and Codex returns concrete failures via revise. Codex chooses acceptance coverage; bridge-run validation is reusable independent process evidence.
5. Review the diff first. Reuse passing checks for identical code/environment. Broaden tests or repeat review only for concrete risk, changed code, failures, missing evidence, or required gates.
6. Corrections identify a defect, scenario and expected behavior. Avoid rewriting the plan or reviewing unchanged areas again. More cycles require measurable progress on an unresolved defect.
7. Read large transcripts only for contradictory evidence, scope/auth problems, missing results, or failed checks.
8. Capture verbose bridge output to the external shadow workspace and extract status, summary, changed paths, checks, usage, and next action; open detailed fields only when needed. Record provider and Codex usage separately when available, plus corrections and accepted outcome. Subscription quota and API-equivalent dollars differ; claim savings only with measurements.
