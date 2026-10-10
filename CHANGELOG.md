# Changelog

## 0.3.0

Pairs with Harness 0.3.0.

### Added

- Add a version stamp and read-only installation health checks with `router doctor` and `install.sh --status`.
- Add a verdict ledger, `router ladder list|reset`, `router lanes --session`, and `router report` for attempts, active work, judge coverage and readable token totals.
- Add local route overlays, `router config`, `router promote` with evidence and dry run, and careful, medium and small overlay presets.
- Add a Claude Code marketplace with Router and shared-roles plugins; generate and check plugin copies, and report plugin state.
- Add release checks, CI, security and contribution guidance, and a tested release-notes script.
- Add upgrade guidance for existing `--with-defaults` installations and shared role ownership.

### Changed

- Check Claude briefs before dispatch, remind once about unjudged returns, block one same-session rerun of a passed brief, and restore lanes after compaction.
- Scope ladder attempts to a project and a 12-hour default window; keep a private ledger with 0600 files and 0700 directory.
- Score and rate-limit role hints, and tighten context nudges with fresh Harness pressure signals.
- Remove unchanged obsolete files on upgrade, preserve edited files, and claim sibling-owned shared roles without rewriting them.
- Make script hooks win over plugin hooks and use plugin-prefixed role names when Claude resolves them.

### Fixed

- Restore original settings bytes and mode across Router and Harness uninstall orders; keep old settings safe during a no-change reinstall.
- Preflight both hosts before installation, report file-named failures and version skew, and guard ledger spool replay and its claim files under contention; log an unsafe ledger spool as one line instead of failing hook calls, and log spool busy and spool full separately.
- Avoid hook bytecode writes, including from the installed router, and report hook loading failures on one line.
- Read plugin role names from project settings at the project root, ignore a relative CLAUDE_PROJECT_DIR, and never treat the Claude home as a project.
- Make `router doctor` run from an install find the checkout and report role skew, or say it cannot.
- Leave settings untouched when the installer reruns after Harness changed them, and update shared roles to the 0.3 bytes once both tools are 0.3.
- Remove the empty folders a 0.1 or 0.2 Router or Harness install made when the last tool leaves.

### Removed

- Remove dispatch-log.py; use the ledger, `router lanes`, and `router report` for run outcomes.

### Upgrade notes

- Run both new installers. A 0.2 installer run after a 0.3 sibling refuses and leaves files unchanged. Use `--dry-run` to preview cleanup and `--purge` to remove Router backups and retired attempts.sqlite3.

## 0.2.0

Pairs with Harness 0.2.0.

### Added

- Add opt-in `install.sh --with-defaults` delegation blocks for Claude and Codex, rendered from routes.json, refreshed by auto mode changes only while the file and block remain, and removed on uninstall while preserving user text, including a missing final newline. Announce the defaults backup path on installation.
- Add shared `router auto off|suggest|nudge|enforce` modes, defaulting to nudge, with a persistent user setting and status output.
- Add optional keyword role hints and main-session read/edit thresholds on both hosts, with spawn resets and documented Codex visibility limits.

### Changed

- Remove Router's empty top-level hooks key on uninstall only when it was absent before first install, including when installed with Harness; preserve it for older manifests and shared hooks.
- Keep large-read protection and other rules with their own modes active when `router auto off` disables automatic routing.
- Adopt Harness's nine shared role files and base Codex TOMLs verbatim.
- Generate tier Markdown and Codex TOMLs from shared bases and routing defaults, with a drift check.
- Use canonical role names for classification, worktree isolation and retry history on both hosts. Codex ladder history from 0.1 restarts after upgrade because its key uses the new role name.
- Claim identical shared files during install and preserve Harness claims during uninstall, failing safe on invalid manifests.
- Group equal delegation return limits from routes.json, remove hook events emptied by Router on uninstall, and name the instruction file when auto mode finds broken markers.
- Check both installation orders with Harness and preserve unrelated settings, hooks and files after each install and uninstall using `HARNESS_DIR=/path/to/harness bash tests/together.sh`.
- Record `asked_type` alongside the routed role so the requested and actual seat remain visible.
- Replace Router's own hook registrations when a reinstall changes their matcher, so reads are not counted twice.
- Drop the Explore, Plan and general-purpose built-in agent overrides; those names become aliases of `researcher`, `planner` and `worker`.
- Count builder, worker, test-writer and docs-writer seats as execution rounds in dispatch-log.py.

### Deprecated

- Previous Router role names are compatibility aliases until a later release. See the README's rename table.
- Codex calls using an uninstalled alias must retry with the canonical name supplied by the hook.

## 0.1.0

- Apply shared up-code checks and routing modes to Codex spawns; add cross-host allow/deny parity regressions.
- Add Codex spawn routing through the shared core, stable role/task ladders, and pinned role TOMLs.
- Add host selection, backed-up Codex hook merging, and hook trust guidance.
- Document encrypted-brief limits; on Codex the brief format is requested through role and dispatch instructions, not hook-enforced.
- Add offline Codex payload tests and isolated fresh-user installation checks for both hosts.
- Accept free-text notes below the four required close-lane brief fields while rejecting duplicates.
- Add routing hooks with explicit agent tiers, a three-attempt execution ladder and a shared kill switch.
- Add sweep, execution and fresh judge seats, built-in agent overrides and the dispatch skill.
- Add bounded waiting, citation checks, a generic run log and worktree scope verification.
- Add an installer with hook merging, backups, dry runs and uninstall support.
- Add example configuration, a fresh-user installation test and standalone regression tests.
