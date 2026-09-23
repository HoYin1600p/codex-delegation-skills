# Delegate to Grok Build

Codex owns scope, planning, usage decisions, validation and final acceptance. Set `CODEX_GROK_BRIDGE` to the full path of the locally authenticated `grok_bridge.py` adapter and use it; Grok Bot is unrelated. See the [shared routing policy](routing-policy.md) for fallback authorization. A failure other than confirmed Claude five-hour exhaustion does not automatically select Grok.

## Launch

1. Create one `READY` assignment matching the Claude bridge `schemas/task.schema.json`: exact committed base, bounded context/changed paths, criteria and validation command; `review_required: true`, `allow_subagents: false`, `require_subscription_auth: true`. Omit router-only fields. Default Grok 4.7 at low effort, initially 3-6 turns.
2. Run `python "$env:CODEX_GROK_BRIDGE" preflight`. Stop on ambiguous account routing, missing executable or signed-out state. If needed, ask the user to run their official Grok CLI login. Never substitute an API key or switch billing.
3. For inherited Claude changes, use its verified `export-handoff` package. Give Grok the exact remainder and the original base. The adapter seeds a separate worktree and verifies identity, file hashes and scope; preserve the source worktree and artifacts.
4. Launch once: `python "$env:CODEX_GROK_BRIDGE" run --task "<task.json>" [--handoff "<handoff-directory>"]`. Wait on the same process; silence never justifies a duplicate. One writing worker at a time.

The adapter uses the write-capable general-purpose agent and internal tool IDs (including `search_replace`). Worker shell execution is unavailable; supply a focused validation command for the adapter to run independently. A no-tools report turn may record an unstructured work result. Treat `not_run` worker checks separately from adapter-run validation evidence.

## Continuation and corrections

Read `result.json`, summary, observed context-read evidence, changed paths, combined diff and validation first. Inspect raw session transcripts only to resolve missing/contradictory evidence or a concrete failure.

For `EXTENSION_REQUESTED`, check progress and the exact remainder. Grok exposes per-session tokens and estimated API-equivalent dollars, not reliable subscription-window percentages; keep headroom unavailable and respect the user's Grok budget. Grant no more than requested, normally at most six turns:

`python "$env:CODEX_GROK_BRIDGE" continue --task "<task.json>" --artifact "<artifact>" --grant-turns <N>`

For completed reviewable work, submit the same feedback shape as Claude: `{"findings":[{"path":"src/file.py","issue":"Concrete defect/scenario","expected_behavior":"Required correction"}]}`.

`python "$env:CODEX_GROK_BRIDGE" revise --task "<task.json>" --artifact "<artifact>" --feedback "<feedback.json>" --grant-turns <N>`

Use 1-6 turns; retain the same session/worktree, original scope and canonical task. Recheck affected behavior after corrections. A valid extension from revision resumes through `continue`.

The adapter can request one additional no-tools checkpoint at a turn cap. For older artifacts blocked solely by that exit, `recover-turn-cap --task "<task.json>" --artifact "<artifact>"` requests only the missing checkpoint after verifying lineage and an unchanged worktree. This is explicit recovery, never an automatic rerun. Malformed checkpoints, timeout, auth/scope conflicts, unsupported failed states, or no progress preserve evidence and stop.

The adapter stores artifacts under the verified Claude Bridge external shadow workspace in `artifacts/grok`. Compare the combined result against the original base, including inherited Claude changes. Completion means `IMPLEMENTED`/`REVIEW_PENDING`; Codex performs final acceptance and any separately authorized integration. Check Claude availability at the next assignment boundary rather than interrupting Grok mid-assignment.
