# Worker Contract

Maintain one canonical assignment matching the bridge schema. Keep router-only backend/reasoning decisions in the launch record, not unsupported JSON fields or a competing plan.

## Assignment

- `task_id`: stable identifier; `plan_status`: `READY` after material decisions are resolved.
- `mode`: normally `implement`; `test` for writing tests. Standalone analysis, research and review stay in Codex.
- `objective`: one coherent verifiable outcome; batch small related changes sharing acceptance criteria.
- `repo_root`, `base_commit`: exact repository and full committed base. Never stash, reset, clean, or commit the primary checkout to prepare delegation. If needed, prepare an isolated private snapshot of authorized current inputs and record its relationship to the primary checkout.
- `context_paths`, `forbidden_context`: minimum useful files/directories and exclusions; allow bounded implementation exploration.
- `allowed_changed_paths`: exact files or supported directory boundaries, including necessary tests.
- `locked_decisions`: approach, interfaces, constraints; routine implementation details belong to the worker.
- `acceptance_criteria`: observable behavior and required evidence. Worker fixes local implementation/test failures before completion.
- `validation_command`, validation timeout: focused deterministic checks. Otherwise specify a manual procedure and expected behavior. Failed/unrun required checks prevent acceptance.
- `risk`: low, medium, high; explain concrete high-risk scenarios in criteria. `review_required`: true for writing tasks, meaning lead acceptance, not a mandatory extra agent.
- `stop_conditions`: scope/auth conflict, missing prerequisite, timeout, or no measurable progress.
- `model`, `max_turns`, `timeout_seconds`: backend settings. Claude initial max 12; Grok normally 3-6. No task-wide extension-count ceiling.
- `allow_subagents`: false; `require_subscription_auth`: true.

Omit deprecated `max_total_turns` and `max_extensions` from new tasks. Revision/handoff CLI inputs hold exact feedback and lineage separately; never modify the canonical task to fake an extension request.

## Results and review

Request compact worker reports: one short summary, check outcomes, and only actionable blockers or exact remaining work. Refer to artifact paths instead of repeating the plan or embedding logs. Read summary, lifecycle, changed paths, diff, checks, blockers and progress first. Evidence must match the actual worktree; do not routinely load transcripts.

- `EXTENSION_REQUESTED`: completed work, exact remainder, reason, requested turns. Codex checks progress/capacity before granting more work in the same session.
- `IMPLEMENTED` / `REVIEW_PENDING`: Codex reviews the change and acceptance evidence.
- Review findings: concrete affected path, defect/scenario, expected behavior. Use `revise` in the same session/worktree, then review the changed behavior and targeted checks.
- `BLOCKED`: preserve evidence and diagnose. Only confirmed five-hour exhaustion permits automatic fallback under the standing policy.
- `CHECKPOINT_FORMAT_FAILED`: use only the selected backend's documented recovery; never rerun implementation because of report formatting.

A revision invalidates checks affected by its changes; it preserves earlier evidence. Handoffs retain the original base and inherited changes, so final acceptance covers the combined result. Codex accepts only after scope and required checks pass.
