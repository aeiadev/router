# Changelog

## 0.2.0

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
