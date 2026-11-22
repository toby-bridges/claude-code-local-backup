---
name: claude-code-local-backup
description: Create and verify private, local backups of explicitly selected Claude Code CLI project sessions and Claude Desktop sessions or scratch workspaces on macOS. Use only when requested for this backup; not for logs, login, credentials, browser data, cloud exports, or cleanup.
---

# Claude Code local backup (macOS)

Use the bundled `scripts/local_backup.py` via `python3 -I` (Python 3.9+), with the script path resolved relative to **this skill**, not the user's current working directory. Do not install packages, contact the network, use other tools to inspect source or copy contents, or search for alternative sources. Do not print directory listings, manifest contents, payloads, file names, or per-file hashes. Do not attach, upload, sync, or commit backups. The CLI script is offline; this does not make the Codex conversation or its normal service connection offline. If even count/status metadata must remain local, give the terminal instructions in `README.md` instead of running through Codex.

## Allowed categories

- `projects`: default Claude Code CLI session tree (`~/.claude/projects`), **not** all code repositories. If a custom `CLAUDE_CONFIG_DIR` is in use, do not fall back to the default path or unset it to continue.
- `desktop-code`: `~/Library/Application Support/Claude-3p/claude-code-sessions`
- `desktop-scratch`: `~/Library/Application Support/Claude/scratch-workspaces`
- `desktop-agent`: `~/Library/Application Support/Claude/local-agent-mode-sessions`

## Workflow

1. Ask the user which categories to include if not explicit. Ask them to save work and quit the relevant Claude apps and CLI writers before planning; never terminate their processes yourself. Remind them that files within a chosen category can contain sensitive text, and to proceed only on a local volume without cloud/sync mirroring of the store.
2. Run `python3 -I <script> plan --item <item>` separately for each selected category. A usable plan has exit 0 and `PLAN item=… files=… bytes=… subdirs=… CONFIRM=…`. Keep the token for the same category; show the user only category, file count, byte count, status, and the fixed destination `~/.claude-code-local-backup/`. A plan reads metadata and opens files for access checks, but does not read file contents or create a store. Exit 3 for missing/empty is a gap, not a successful backup; exit 2 is blocked. Report every gap.
3. **Stop and wait for explicit confirmation of the reported write scope in a new user response.** Prior requests to back up, a plan token, or permission to run a tool do not supply this confirmation. After confirmation, for each approved and plannable category run `python3 -I <script> backup --item <item> --confirm <plan-token>`. Do not manufacture or refresh a token to bypass confirmation. Require exit 0 and `BACKED_UP item=…` together, keeping the exact `BATCH=<16 lowercase hex digits>` from the same run. The script writes only its private store and never rehomes or deletes sources.
4. For each successfully written category run `python3 -I <script> verify --backup <batch-id>`. Require exit 0 and `VERIFIED item=…` for the correct category. If anything is blocked (including the Codex sandbox), stop writes, report the reason, retain original and any incomplete batch, and do not retry with elevated privileges, disable the sandbox, use an alternate source, or delete a partial batch. For stale plans, re-plan and obtain a new confirmation before writing again. If needed, give the user the same documented commands to run themselves in a normal terminal; do not claim an unrun command succeeded.
5. Ask the user to open one recognisable **copy** for each verified category: in Finder go to `~/.claude-code-local-backup/<batch-id>/copy`, copy a familiar file to a private local working folder, and open the duplicate in a trusted local viewer (do not execute it). Do not open it for them and do not request filenames, contents, or screenshots. Record the category, counts, batch ID, `VERIFIED`/blocked status and manual result (`opened`, `failed`, `not checked`). A hash match is not a recovery or manual-opening guarantee. Do not mark unchecked categories complete.
