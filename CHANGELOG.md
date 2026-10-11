# Changelog

## 0.3.1

Pairs with Harness 0.3.1. If the installed Harness is older, the installer prints one upgrade notice; upgrade both tools.

### Fixed

- Treat Windows permission bits consistently on Python 3.13, which exposes `fchmod` but cannot enforce private POSIX modes.
- Fall back from incomplete UNC `XDG_STATE_HOME` values consistently in the launcher and Python runtime across Python 3.9 and 3.13.

- Run the hooks and the `router` command on Python 3.9: the shared helpers no longer fail at import on `X | None` annotations.
- `router` no longer needs `/usr/bin/env -S` (busybox and coreutils before 8.30 lack it), and no entry point writes `.pyc` files.
- Keep switch files, the ledger and the Harness pressure signal working when HOME, ~/.local or XDG_STATE_HOME sits behind a symlink.
- Read settings and manifests that start with a UTF-8 byte order mark; the installer still writes them LF without one.
- Give every settings backup a name no file holds yet: when the clock moves in coarse ticks (Windows Python 3.12), two backups in one run shared a name and the original's copy was replaced.
- Lock state on a separate `.lock` file (flock or msvcrt, polled for 2 seconds); when the lock stays held, the hook skips that update instead of waiting.
- Lock the ledger spool through `ledger.spool.lock` and rename it only while no writer has it open, which Windows requires.
- Run the BAR of `scripts/close-lane.sh` in the bash that runs the script, never a bare `bash` that native Windows resolves to the WSL one; a BAR timeout on Windows ends its process tree.
- `scripts/wait-until.sh` on native Windows no longer kills the process it watches: `pid` asks Windows whether the process has exited, `no-proc` reads the process table itself, and a Git Bash `$!` pid is mapped to its Windows pid.
- `scripts/gen_plugin.py --check` compares bytes only on Windows, which keeps no execute bit, so a Windows checkout no longer reports `plugins/router/bin/router` as drift.
- `router doctor` reports `[ok] path` on native Windows when the installed `router` is first on PATH, and reads old Windows manifest paths when it checks roles and shared-role skew.

### Platforms

- Run on Linux, stock macOS (system Bash 3.2 and `/usr/bin/python3` 3.9), WSL and native Windows through Git for Windows, with Claude Code or Codex. Python 3.9 is the floor in the installer, `router doctor` and the README.
- Start every hook, `router` and the helper scripts through `hooks/router/pyrun.sh`, a POSIX sh launcher. It uses `$ROUTER_PYTHON`, the saved interpreter, then the first of `python3`, `python` and `py -3` that is 3.9 or newer, never the Windows Store stub, and sets UTF-8. With no Python, a hook prints one line on stderr and exits 1.
- Register hooks as `sh "$HOME/.claude/hooks/router/pyrun.sh" router warn <hook>.py`, with quoted absolute `C:/` paths on Windows and for a home with spaces. On Windows, Codex hooks also get a PowerShell `command_windows` pinned to the installer's Python. `router doctor` recognises the new and the older hook form.
- Load the hooks and `router` on native Windows Python: `hooks/router/compat.py` holds every POSIX-only call (file lock, open flags, owner check, chmod, atomic replace, pread, home lookup) with a Windows path.
- Install into `$HOME` on native Windows (Windows Python's Path.home() reads USERPROFILE only), and detect a host CLI that is on PATH without a PATHEXT extension, such as an npm or Git Bash shim.
- Where the account cannot create symlinks, install a copy of each shipped link's target, record it by sha256, and handle links and copies alike on upgrade and uninstall. Links use the native separator; an upgrade rewrites a `/` link that Windows left dangling.
- Write manifest paths with `/` on every OS and read the backslash paths an older Windows install wrote, so a shared role Harness 0.1 or 0.2 claims is kept instead of refused.
- Upgrade over a Router 0.1 or 0.2 install on Windows: the installer clears the read-only flag those releases left on its own files.
- Keep a Windows checkout LF (`.gitattributes`, SQLite fixtures binary), hash shared role sources with CRLF read as LF, and stop `install.sh` on a CRLF copy with one line naming the fix.
- Open state files in binary mode and write them LF; a lane label that starts with the home folder in any Windows form shows `~`.
- Not enforced on native Windows: POSIX owner checks and file modes such as 0600 (NTFS keeps none); the installer skips chmod there.
- Codex hooks on native Windows run the Python that ran `install.sh`; after moving or replacing it, rerun `install.sh`.
- Control-character file names and paths beyond the 260-character MAX_PATH are outside native Windows support.
- State paths written as `~user/...`, and on native Windows a `~/` path such as `~//tmp` or `~/C:state`, may need `ROUTER_PYTHON` or a Python on PATH; they cannot start from the saved interpreter alone.
- On native Windows, upgrade both tools to 0.3.1 before uninstalling: a 0.2 uninstaller can misread current manifest keys. Upgrading alongside Harness 0.2 there is not supported.

### Changed

- Run CI on ubuntu, macOS and Windows (Git Bash) at Python 3.9 and 3.13, with fail-fast off, so one failing job no longer cancels the rest.
- Run every test file through `tests/run_all.py` in CI and from CONTRIBUTING.md: one line per file, a per-file timeout, the output of each failure and a final list of failures. Router and Harness ship the same file.
- Run the tests under Git Bash on native Windows: `tests/_platform.py` runs bash by full path (never the WSL bash.exe in System32), puts a `python3` shim script on PATH, ends a timed-out test's whole process tree and prints the reason for each skip. `PYTHONPATH=tests/win_sim` runs the Windows code paths on Linux and macOS.
- Build the 0.1 and 0.2 release trees from commit SHAs in the test and together jobs.
- Pick the Harness commit for the together and shared-drift jobs with `scripts/harness-ref.sh resolve`: HARNESS_REF when set, else the Harness `v<VERSION>` tag, else Harness main once its VERSION is the same or newer. While a paired release is still being pushed, retry every 20 seconds for up to 10 minutes.
- Test more of the installed behaviour: the context guard with an urgent signal, `tests/together.sh` with pinned XDG_CONFIG_HOME and ROUTER_LOCAL and a single skew notice, an unreadable shared role, a claim file swapped while being read, and uninstalling directly over a Router 0.1 install.
- Check every shipped Python source against the 3.9 grammar, union annotations without the future import, and 3.10+ library names.
- State the platforms, requirements and limits in the README, and correct how Router picks the pressure host: `harness_host`, then `--host`, then `turn_id`.

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
