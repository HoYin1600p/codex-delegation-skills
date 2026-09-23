# Codex delegation skill

A single `delegate-work` skill for repository implementation. Codex scopes work and owns design decisions, review and acceptance. Claude implements code and tests while its subscription has capacity. Grok can continue preserved work after confirmed Claude five-hour exhaustion.

## Install

Install the one skill with the [Skills CLI](https://skills.sh/docs/cli):

```powershell
npx skills add HoYin1600p/codex-delegation-skills --skill delegate-work --global
```

The repository is private for its initial review. GitHub access is required to install it while private.

## Bridge prerequisites

The skill includes one entry point plus supporting procedure and policy references. It does not bundle the local Claude and Grok bridge programs.

Before using the Claude procedure, set `CODEX_CLAUDE_BRIDGE_ROOT` to the directory containing the bridge `scripts/claude_bridge.py` and `schemas/` files. Before using the Grok procedure, set `CODEX_GROK_BRIDGE` to the full path of the locally authenticated `grok_bridge.py`. Install/configure those bridges separately and review their access, authentication, usage gates, worktree isolation, and artifact retention before connecting them.

The Grok bridge must use the signed-in subscription route. The skill does not authorize API-key billing or production access. Repository instructions and the user's authorization govern every delegated task.

## License

No license is granted by this private initial repository. Choose a license before making it public.
