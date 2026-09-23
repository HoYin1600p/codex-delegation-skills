# Delegate to Claude Code

Codex owns planning, review and acceptance; Claude owns bounded implementation and corrections. Use the bundled `scripts/claude_bridge.py` for every provider operation. Resolve `$SkillRoot` and `$Python` using [setup](setup.md). Routing and review depth are in the [shared routing policy](routing-policy.md).

## Assignment and launch

Use the bridge's `schemas/task.schema.json` with a full committed base, named context, permitted changed paths, locked decisions, observable criteria and focused validation. Set `review_required: true`, `allow_subagents: false`, and `require_subscription_auth: true`. Omit deprecated total-turn/extension-count limits. Initial work is normally 8-12 turns (maximum 12), shorter for small work or little capacity.

Uncommitted primary changes are not copied. Never stash/reset/clean or commit the primary checkout to prepare work. The lead may prepare an isolated snapshot when authorized current changes must be included; keep it and support files in the verified external shadow workspace.

`& $Python -B "$SkillRoot/scripts/claude_bridge.py" run --task "<task.json>"`

Launch once and wait on the same process. Silence is not a retry signal. Poll foreground sessions no more than once every 45-60 seconds. Capture verbose output in the external shadow workspace and surface the concise result/next action. Do not load full transcripts routinely.

Claude has no shell tool. Supply `validation_command` so the bridge runs the requested checks independently. Claude reports unavailable checks as `not_run`; Codex then uses the bridge validation record, not that worker claim, for acceptance. Send concrete validation failures back through `revise` when its state checks allow it.

## Usage and continuation

The bridge checks five-hour capacity at launch and before continuation, revision, and checkpoint repair. Verified capacity above zero is usable; zero/exhausted blocks work. The former 5% reserve is removed. Legacy override flags do not authorize exhaustion or unverifiable usage.

`& $Python -B "$SkillRoot/scripts/claude_bridge.py" usage`

Prefer a recent usage snapshot already returned by the bridge; run `usage` separately when routing needs a reading and no suitable recent snapshot exists. The next bridge work operation still enforces its own gate. Use five-hour and weekly used/remaining values, reset times, and retrieval mode/age. Report both windows succinctly; repeat reset details only when changed or relevant. Weekly/model-specific readings are advisory unless the provider actually refuses work. Reuse the bridge's 60-second cache and bounded transient retry. Its labeled stale fallback is usable only under its existing age/headroom restrictions; disclose age. An expired-token official-CLI refresh must be disclosed when reported. Do not add immediate manual retries to a failed usage request. Unknown usage pauses work and does not trigger Grok.

For `EXTENSION_REQUESTED`, inspect completed work, exact remainder, reason, requested turns, and progress. Check usage, then grant no more than requested, normally 8-12, shorter near exhaustion:

`& $Python -B "$SkillRoot/scripts/claude_bridge.py" continue --task "<task.json>" --artifact "<artifact>" --grant-turns <N>`

Continue the same session/worktree while meaningful progress and capacity remain. No task-wide turn/extension ceiling. Older `CAP_EXHAUSTED` artifacts with valid extension requests remain eligible. Do not take implementation back into Codex merely because multiple extensions were needed.

For `CHECKPOINT_FORMAT_FAILED`, inspect `continuation.can_repair_checkpoint` and usage, then use the bounded same-session no-tools repair once:

`& $Python -B "$SkillRoot/scripts/claude_bridge.py" repair-checkpoint --task "<task.json>" --artifact "<artifact>"`

This repairs reporting only. A repeated failure preserves evidence and stops; never rerun the assignment. A turn cap with a valid checkpoint is an expected segment boundary.

## Review corrections and handoff

For a completed reviewable result, or a result blocked solely by independent validation failure, use a feedback JSON file shaped as:

`{"findings":[{"path":"src/file.py","issue":"Concrete defect and triggering scenario","expected_behavior":"Required correction"}]}`

`& $Python -B "$SkillRoot/scripts/claude_bridge.py" revise --task "<task.json>" --artifact "<artifact>" --feedback "<feedback.json>" --grant-turns <N>`

Use 1-12 turns. A paused `last_revision_decision` means no correction ran even if the preserved prior result still says complete; inspect the decision and usage gate. Findings must stay within original permitted paths. The bridge verifies lifecycle, session, fingerprint, scope and usage; preserve the canonical task and prior evidence. Do not fabricate an extension request. If revised work needs more time, handle its valid extension normally. Review affected behavior and check evidence instead of restarting the full review.

After confirmed five-hour exhaustion, with Claude no longer writing, export preserved partial changes for Grok:

`& $Python -B "$SkillRoot/scripts/claude_bridge.py" export-handoff --task "<task.json>" --artifact "<artifact>" --output "<handoff-directory>"`

Use the verified package with Grok's `run --handoff`; never discard partial work or transfer a failed session wholesale. Failed integrity checks require diagnosis. See the shared routing policy for precise fallback conditions.

Inspect summary, diff, changed paths and independent validation before acceptance. Completion is `IMPLEMENTED`/`REVIEW_PENDING`, never automatic acceptance or publication. Scope/auth violations, timeout, unavailable usage, unsupported failed states and no progress preserve evidence and block work. For a forcibly terminated launcher, inspect `active --task-id <id>`; `clear-stale-lock --task-id <id>` is explicit recovery only after the process is proven dead.
