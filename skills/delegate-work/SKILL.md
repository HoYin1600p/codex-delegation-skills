---
name: delegate-work
description: Coordinate repository implementation with Claude as the primary subscription worker, Codex for planning and acceptance, and Grok after confirmed Claude five-hour exhaustion. Use for code changes or conserving Codex usage; handle advice and read-only review in Codex.
---

# Delegate Work

While the user retains the Claude subscription, Claude implements code, tests and review corrections. Codex owns requirements, diagnosis, design, scope, review, acceptance and authorized integration. Grok is the implementation fallback after confirmed five-hour exhaustion or explicit user selection. Native Codex helpers perform justified read-only work.

1. Read repository instructions and inspect enough evidence to settle the approach and scope. Reuse established context; let Claude explore named implementation areas and resolve local details. Do not pre-solve its code or repeat its exploration.
2. Apply [references/routing-policy.md](references/routing-policy.md) for review depth, capacity fallback and token discipline. The current lead normally plans and reviews directly. Batch small related changes sharing an acceptance boundary.
3. Create one compact `READY` assignment using [references/worker-contract.md](references/worker-contract.md): observable criteria, minimum useful context and focused validation.
4. Read only the selected provider procedure: [Claude](references/claude-workflow.md) or [Grok](references/grok-fallback.md). Its bridge controls launch, continuation, revisions and handoff artifacts. Keep one writing worker; use the same session for progress and corrections. Never invoke provider CLIs directly or duplicate a quiet task.
5. Review summary, changed paths, diff and check evidence first. Reuse valid checks for unchanged code/environment. Return concrete findings through `revise`, then recheck affected behavior. Worker completion requires Codex acceptance; failed or missing required checks prevent it.

Reuse already-read skill guidance within the conversation unless it changes. Preserve evidence and follow backend recovery on errors; only confirmed five-hour exhaustion permits automatic fallback. Repository instructions, public identity checks and production authorization still govern integration/release.

Report outcome, checks, blockers, artifact location and measured usage concisely. Provider and Codex usage are separate; unavailable metrics remain unavailable.
