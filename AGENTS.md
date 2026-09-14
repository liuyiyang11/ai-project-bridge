# AI Project Bridge repository instructions

This repository implements a local task bridge. Its security boundary is more important than convenience.

- Never add arbitrary shell, PowerShell, Python-expression, or remote command execution.
- Every new executor must validate its task schema before doing work.
- Resolve every remote path under the registered project root and reject traversal/absolute paths.
- Never automatically merge a pull request, push `main`/`master`, or force-push.
- Do not read, store, or modify GitHub, browser, SSH, or Codex authentication credentials.
- Keep real project roots, dataset paths, and private settings in ignored `config.local.yaml` only.
- Managed projects may have their own `AGENTS.md`; the Bridge must not overwrite it.

Use fake GitHub/Codex adapters for automated tests. A real Codex call is an explicit opt-in smoke test.

