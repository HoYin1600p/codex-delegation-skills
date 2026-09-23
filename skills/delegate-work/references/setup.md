# First-use setup

Codex performs these checks and handles mechanical setup. The recipient must complete their own browser sign-in and have provider access; credentials cannot be included in a shared skill. Do not call an installation ready for work until its authenticated checks pass. An offline installation test does not establish account readiness.

## Supported environment

This release is verified on native Windows with local Codex, Python 3.11+, Git, native Claude Code, and optional native Grok Build. It is not verified for macOS, WSL, Linux or hosted Codex cloud tasks. Claude usage currently relies on the native Windows CLI's credential-file layout; successful login through a different credential store is insufficient.

Locate Python 3.11+ on PATH or use Codex's bundled Python when workspace dependency discovery is available. Set `$Python` to its full executable path and `$SkillRoot` to the directory containing this skill's `SKILL.md`. Resolve both from the current environment; never copy a publisher's machine paths. Both provider bridges and all schemas are bundled. No pip packages, extra repositories, or bridge-path environment variables are needed.

If Git or the provider CLI is missing, Codex should follow the current official installers: [Git for Windows](https://git-scm.com/downloads/win), [Claude Code](https://code.claude.com/docs/en/setup), and [Grok Build](https://docs.x.ai/build/overview). Use native executables. Installing the Claude/Grok desktop chat applications alone is insufficient. Reopen Codex after PATH changes if needed; the launchers also discover the standard native install directories.

## Verify the package

```powershell
& $Python -B "$SkillRoot/scripts/setup.py" doctor --offline
& $Python -B "$SkillRoot/scripts/setup.py" self-test
```

The offline test exercises a disposable Git fixture, both launchers, task validation, command/schema loading and independent validation. It does not contact either provider. A nonzero exit includes a specific next action; Codex should resolve the stated prerequisite before attempting a task.

## Sign in and verify both routes

Use `claude auth login` to sign in with the recipient's Claude subscription. For fallback, use `grok login` with their Grok account. Codex can open the official flow, but the account owner completes browser authentication. Never request pasted tokens, read credentials into the conversation, copy another account's state, or substitute API keys.

```powershell
& $Python -B "$SkillRoot/scripts/setup.py" doctor --require-grok
```

The doctor checks subscription routing, CLI capabilities, Claude quota access and availability of Grok 4.7. It emits concise readiness and corrective actions, without account emails, IDs, tokens or full auth output. Claude-only use can run `doctor` without `--require-grok`; clearly disclose that fallback is unavailable until the Grok check succeeds. Confirmed Claude five-hour exhaustion can legitimately leave Claude capacity unavailable; verify Grok before applying the routing policy. Missing/unknown usage is not exhaustion.

The actual work launch checks authentication and quota again. The native CLI and usage endpoint can change independently of this package; unsupported flags, unavailable quota or missing account access must produce a clear stop, never a fabricated successful setup. After login and capacity verification, a small live smoke test is available through `claude_bridge.py smoke`; it uses subscription capacity and verifies a real reply. It is separate from the offline test.

## Private runtime storage

By default, logs, task files, worktrees, quota caches and handoff packages stay under the current user's Codex home in `delegate-work-state/<installation-hash>/`. A local marker prevents mixing installations. No state is stored inside the installed skill. To honor a repository's prescribed external workspace, set `DELEGATE_WORK_STATE_DIR` to a fresh absolute subdirectory there before running either bridge. The variable must be available to every subsequent bridge command for that task.

Never place state inside a source repository or the installed skill. Never include runtime state, provider directories, account configuration, `.env` files, session exports, logs, or caches in Git commits or release archives. Live provider/validation output can contain private data even though no credentials ship in this package.

For assignments, start from `assets/task.template.json`, fill all actual task values, and save the file outside the working repository. Keep `model: null` to use the account's selected Claude model. Grok selects its own verified provider model while preserving the canonical task during handoff.
