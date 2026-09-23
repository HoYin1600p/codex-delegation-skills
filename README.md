# Codex delegation skill

One `delegate-work` skill: Codex plans, reviews and accepts; Claude implements and corrects; Grok continues preserved work after confirmed Claude five-hour exhaustion. Both bridge programs, schemas, task template and setup checks are included.

## Install in Codex

Give Codex this request:

> Install the skill from https://github.com/HoYin1600p/codex-delegation-skills/tree/main/skills/delegate-work. Follow its setup guide, run the offline self-test, and verify Claude and Grok readiness. Handle setup for me and guide me through any browser sign-in needed.

Codex's skill installer can install that complete directory. While this repository is private, the recipient's GitHub account must have repository access. An inaccessible private repo is not a skill installation failure.

Alternatively, after downloading or cloning the complete repository, run `python install.py` with Python 3.11+. This copies the allowlisted skill files to `~/.agents/skills/delegate-work`, the documented Codex user skill directory. It refuses to overwrite an existing installation. `--destination` selects another explicit skill directory for evaluation. Then ask Codex to use `$delegate-work` to finish setup. If the skill does not appear, restart Codex.

## Requirements and first run

The verified target is **native Windows with local Codex**, Python 3.11+ and Git. The Python runtime has no third-party package dependencies. Codex can use its bundled Python where available. Install the native [Claude Code CLI](https://code.claude.com/docs/en/setup) and sign in with your own Claude subscription. For fallback, install native [Grok Build](https://docs.x.ai/build/overview), sign in with your own account, and verify access to Grok 4.7. Desktop chat applications alone do not provide these CLIs.

Codex follows the included [setup guide](skills/delegate-work/references/setup.md). It runs `scripts/setup.py doctor --offline` and `self-test`, then `doctor --require-grok` to verify actual provider readiness. The offline checks do not contact AI services. Authenticated doctor checks do not generate model work. Browser sign-in, subscription access, account capacity and provider availability must come from each recipient; the package cannot supply them.

Do not claim macOS, Linux, WSL, hosted-cloud execution or an untested provider version is ready. Readiness is checked before work, and unsupported environments receive a specific setup result.

## Privacy and behavior

No provider accounts, credentials, configuration, sessions, quota caches, or local work artifacts are included. Bridges use the recipient's existing CLI authentication. API-key billing routes are rejected. Runtime state stays in private external storage, with a configurable location for repository workspace rules. Keep runtime state out of Git and release archives.

Provider completion requires Codex review and focused validation. Sessions continue and accept review corrections without arbitrary task-wide turn ceilings. Unknown quota, login errors and generic failures do not trigger automatic Grok fallback. Worktrees and partial changes are retained for verified handoff. Repository instructions and the user's authorization govern integration and publication.

See [workflow](skills/delegate-work/SKILL.md), [routing policy](skills/delegate-work/references/routing-policy.md) and [worker contract](skills/delegate-work/references/worker-contract.md). Codex's [official skill documentation](https://learn.chatgpt.com/docs/build-skills) describes discovery and installation.

## Verification

The Windows release audit exercised installation into an empty user profile with the original bridges and provider accounts unavailable, both offline launchers, missing-provider setup guidance, and exclusion of account/state files. The regression suite passed 119 tests; one symbolic-link test was skipped because Windows lacked the required privilege.

An independently installed copy passed authenticated readiness checks with Claude Code 2.1.280 and Grok Build 1.0.41. Claude completed both a live smoke check and a bounded implementation in a disposable repository; the bridge's two independent fixture tests passed and the original checkout remained unchanged. Grok handoff, revision and recovery are covered by regression tests; a live Grok implementation was not run during this audit. These checks establish packaging and the tested environment, not another person's subscription entitlement or future provider compatibility.

To run the repository regression suite in PowerShell, set `$env:PYTHONPATH` to this repository's `skills/delegate-work/src`, set `$env:PYTHONDONTWRITEBYTECODE='1'`, then run `python -B -m unittest discover -s tests -v`. Direct temporary files to your external workspace when repository rules require it.

## License

No license has been selected for this private review repository. Select one before public distribution.
