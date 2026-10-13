# Router

Route simple work to cheaper models and harder work to stronger models. The owner has each returned result judged by a fresh seat; Claude Code checks repository briefs before a run, and active lanes are restored after compaction.

Router routes bounded tasks in Claude Code and Codex CLI. Pair it with [Harness](https://github.com/aeiadev/harness) for checkpoint restore and context pressure signals.

## Demo

Give a builder this brief:

```text
TASK Check parser empty input.
FILES src/parser.py tests/test_parser.py
RETURN Five lines: CHANGED / BAR / OUTPUT / NOT DONE / OPEN.
```

Claude Code denies the spawn before it costs a round: `brief: BAR missing (builder needs TASK, FILES, BAR, RETURN in that order)`. With a complete brief, inspect the run from an installed checkout:

```bash
router lanes
router report
router doctor
```

Stable lines in a fresh HOME after `bash install.sh --host both`:

```text
no lanes
codex lanes: 0 (no verdicts on Codex)
[ok] spawn: dry run allowed a sweeper brief
```

A seat is a named agent role. The owner is the session coordinating the work. Each task has a BAR: the acceptance command that checks its result.

## How a brief flows

Split repository work only when it divides cleanly, fits a clear four-line brief, has a verifier and saves more effort than delegation costs. For a service or other non-Git target, use the opt-in [operations profile](docs/operations.md) in 0.4.0.

1. For repository work, the owner writes `TASK`, `FILES`, `BAR` and `RETURN`.
2. Haiku handles a bounded read-only sweep. Sonnet builds in its own Git worktree.
3. The worker returns five lines: `CHANGED`, `BAR`, `OUTPUT`, `NOT DONE`, `OPEN`.
4. A fresh Opus judge reads the brief and result, inspects the files and runs the BAR itself. Its verdict is `PASS` or `SEND_BACK`. It never fixes the work.
5. The owner accepts a verified result or retries with the concrete failure. The attempt ladder allows two standard attempts, then one upper-tier attempt.

The dispatch skill guides this workflow. The host-specific hooks enforce the checks listed below; the owner still starts the judge and decides whether to accept the result.

<!-- shared:begin -->
### Install both

[Router](https://github.com/aeiadev/router) routes bounded work to model tiers, checks Claude briefs before a run, and tracks lanes and verdicts. [Harness](https://github.com/aeiadev/harness) guards commands and restores project context. Together, they share roles and add a context pressure signal; each prints its own restore content, so they never write the same line twice.

Clone and install each checkout in either order:

<!-- docs:skip -->
```bash
git clone https://github.com/aeiadev/router.git
cd router && bash install.sh --host both
```

<!-- docs:skip -->
```bash
git clone https://github.com/aeiadev/harness.git
cd harness && bash install.sh --host both
```

For Codex, start a new session and review and trust the hooks. Changed scripts can prompt another trust review.

### Upgrade

Run `install.sh` from the new checkout of each tool. An unchanged old file that is no longer shipped is removed and listed; an edited one is kept and listed. `--dry-run` previews changes. Shared roles that the sibling already installed are claimed by this tool, never rewritten. If the sibling is older, the installer prints one notice; upgrade both to get the new shared roles. A 0.2 installer run after a 0.3 sibling still refuses and leaves files unchanged, so upgrade both.

Uninstall restores the original settings bytes and removes only files the tool created. Backups stay until `--purge`: alone, it lists and removes that tool's own backups; Router also removes its retired attempts.sqlite3. With `--uninstall`, it uninstalls then purges; with `--dry-run`, it lists only. Harness `install.sh --status` and `router doctor` show installed versions and skew; Router `install.sh --status` reports Claude Code plugin state.

From 0.1 or 0.2: those releases did not record which backup held your settings. Uninstall restores the oldest Router or Harness backup only when it holds exactly the settings left once both tools are removed; otherwise it writes that content in standard JSON layout, or removes the file when no backup is left and nothing else remains. If Harness 0.1 or 0.2 went first, the file stays 0600.

### Claude Code plugin route

In Claude Code, `/plugin marketplace add aeiadev/router`, then `/plugin install router@router` and `/plugin install shared-roles@router`. The settings form is `{"enabledPlugins": {"router@router": true}}`. Plugin agents use names such as `router:builder-std`. A script install wins over the plugin: plugin hooks stand down when the install manifest exists. Codex has no plugin; it is not shipped there.
<!-- shared:end -->

## Platforms

Requirements: Bash 3.2 or newer, Python 3.9 or newer, Git, and Claude Code or Codex CLI with subagents and command hooks; on native Windows, Git for Windows, with `install.sh` run from Git Bash. The Codex adapter uses the spawn hook format as of Codex CLI 0.156. Hook state uses standard-library SQLite and file locks.

| Platform | Claude Code | Codex CLI | Requirements |
| --- | --- | --- | --- |
| Linux | Hooks start with `sh` | Hooks start with `sh` | Bash 3.2+, Python 3.9+, Git |
| Stock macOS | Hooks start with `sh` | Hooks start with `sh` | System Bash 3.2, `/usr/bin/python3` 3.9, Git |
| Native Windows | Hooks run in Git Bash | Hooks run in PowerShell through `command_windows` | Git for Windows; Python 3.9+ as `python3`, `python` or `py -3` |
| WSL | As Linux, inside WSL | As Linux, inside WSL | As Linux; separate from a native Windows install |

Every hook and the `router` command start through `hooks/router/pyrun.sh`, a POSIX `sh` launcher. It uses `ROUTER_PYTHON` when set, else the interpreter saved in Router's state folder, else the first of `python3`, `python` and `py -3` that is Python 3.9 or newer. Once Router's state folder exists, the first run that finds Python, even a `--dry-run`, saves its path there as `python`. With no Python, `install.sh` stops with `router install: missing requirement: python3`, and a hook prints this line on stderr and exits 1:

```text
router: no Python 3.9 or newer found (python3, python, py -3); install one or set ROUTER_PYTHON
```

### Native Windows limits

- POSIX owner checks and permission bits such as 0600 and 0700 are not enforced on native Windows (NTFS keeps no POSIX modes), and the installer skips chmod there. Use Windows account and folder permissions to control access. File-mode statements elsewhere in this README apply to Linux, macOS and WSL.
- Where the account cannot create symlinks (Windows without Developer Mode), the installer copies Router's internal links instead. The manifest records each copy by sha256, and upgrade and uninstall handle copies and links alike.
- Codex runs hooks on native Windows in PowerShell through `command_windows`, with the Python that ran `install.sh`. If that Python is removed, the hook prints `router: Python missing; rerun install.sh` and exits 1; rerun `install.sh` after moving or replacing Python.
- The Microsoft Store `python3` stub never counts as Python. With no real Python, Router prints the missing-Python line above instead of failing silently.
- Use Windows-valid file names without control characters; names that Windows cannot create are outside native Windows support.
- Keep installation, state and backup paths below the usual 260-character MAX_PATH limit, including the files created beneath them; longer paths are outside native Windows support.
- Native Windows support starts with 0.3.1. Upgrade both Router and Harness to 0.3.1 before uninstalling either: a 0.2 uninstaller predates Windows support and can misread the current sibling's manifest keys.
- Upgrading alongside Harness 0.2 on native Windows is not supported: Harness 0.2 claims no shared roles there, so Router refreshes them without the older-sibling notice.

Native Windows and WSL keep separate installations; install in each one you use.

## Install

Pair with [Harness](https://github.com/aeiadev/harness) for guarded commands and context restore; the shared Install both and Upgrade block above covers both tools. Check [Platforms](#platforms) for requirements.

Use the [Router 0.4.0 release](https://github.com/aeiadev/router/releases/tag/v0.4.0)
for this version. From its checkout:

<!-- docs:skip -->
```bash
bash install.sh --host both --dry-run
bash install.sh --host both
export PATH="${CLAUDE_HOME:-$HOME/.claude}/router/bin:$PATH"
router status
```

For Claude Code, the installer copies agents to `~/.claude/agents`, the skill to `~/.claude/skills/dispatch`, hooks to `~/.claude/hooks/router`, and the CLI and helpers under `~/.claude/router`. It merges the hook registrations into `settings.json`, creates that file when absent, and backs up an existing file before changing it. Repeated installation does not duplicate registrations or remove unrelated settings and hooks.

Use `--host claude`, `--host codex`, or `--host both`. With no `--host`, the
installer detects existing configuration homes, explicit `CLAUDE_HOME` or
`CODEX_HOME`, and executables on PATH without starting either host. When neither
is present, the installer uses Claude as the default.

For Codex, it copies [role TOMLs](codex/agents) to the global `~/.codex/agents/`, the
skill to `~/.codex/skills/dispatch`, the shared hooks to `~/.codex/hooks/router`, and
helpers to `~/.codex/router/bin`. It merges [the hook template](codex/hooks.json) into
`~/.codex/hooks.json`, backing up an existing file and preserving unrelated hooks.
Set `CODEX_HOME` to use another location. For Codex-only installs:

<!-- docs:skip -->
```bash
bash install.sh --host codex
export PATH="${CODEX_HOME:-$HOME/.codex}/router/bin:$PATH"
router status
```

Start a new Codex session and **review and trust the new hooks** when prompted.
Codex will not run new or changed hooks until they are trusted. Script changes can
require another trust review. The installer does not bypass that gate or run Codex.

It refuses to overwrite differing pre-existing files or locally modified package files. Back up and resolve those conflicts before reinstalling. Uninstall preserves modified files, unrelated hooks and settings, backups and run state.

For another configuration directory, export `CLAUDE_HOME` before installing and keep it set when using Router:

<!-- docs:skip -->
```bash
export CLAUDE_HOME="$HOME/.claude-test"
bash install.sh
export PATH="$CLAUDE_HOME/router/bin:$PATH"
```

Review [the settings example](examples/settings.example.json) for the hooks block. Without `--with-defaults`, the installer never edits `CLAUDE.md` or `AGENTS.md`; with it, see [Delegation defaults](#delegation-defaults). If pasting [the instruction snippet](examples/CLAUDE.md.snippet) by hand, paste without the markers. Start a new Claude Code session after installation so the agents and skill are loaded.

<!-- docs:skip -->
```bash
bash install.sh --host both --uninstall --dry-run
bash install.sh --host both --uninstall
```

## Quick start

In Claude Code, invoke `/dispatch`; in Codex, invoke `$dispatch`. Supply a bounded task or this brief:

```text
TASK Add a test for the parser's empty-input case.
FILES src/parser.py tests/test_parser.py
BAR timeout 60 python3 tests/test_parser.py
RETURN Five lines: CHANGED / BAR / OUTPUT / NOT DONE / OPEN.
```

Use paths relative to the repository root. Keep `FILES` narrow and make `BAR` a real command that proves the task. The worker runs it, then a fresh `judge` runs it again in the result's worktree. A sweep can use the same format with read-only files and a check that verifies its answer.

For example, send this brief to Claude Code with `/dispatch`:

```text
TASK Check the parser's empty-input behavior.
FILES src/parser.py tests/test_parser.py
BAR timeout 60 python3 tests/test_parser.py
RETURN Five lines: CHANGED / BAR / OUTPUT / NOT DONE / OPEN.
```

In Codex, send the same brief after invoking `$dispatch`. This simple check routes to the sweep seat: Claude Code's hook rewrites `sweeper` to `sweeper-light` (Haiku); Codex keeps `sweeper` (gpt-6-luna, low). Each decision is logged with `run_type` and `tier` in `~/.local/state/claude-router/spawns.jsonl` by default (`ROUTER_STATE` or `XDG_STATE_HOME` moves it).

| Seat | Claude default | Codex default | Job |
| --- | --- | --- | --- |
| `sweeper` | Haiku | gpt-6-luna, low | Bounded searches, read-only |
| `builder` | Sonnet | gpt-6-sol, medium | Build in an isolated worktree |
| `builder-in-place` | Sonnet | gpt-6-sol, medium | Execute in an assigned directory |
| `judge` | Opus | gpt-6-astra, high | Fresh verification, read-only |
| `researcher`, `planner`, `worker` | Sonnet | gpt-6-sol, medium | Research, planning, bounded work |
| `test-writer`, `docs-writer` | Sonnet | gpt-6-sol, medium | Focused tests and documentation; execution class, no tier files |

### Renamed in 0.2

| Old | New |
| --- | --- |
| `seat-exec` | `builder` |
| `seat-exec-here` | `builder-in-place` |
| `seat-judge` | `judge` |
| `seat-sweep` | `sweeper` |

The legacy `seat-*` names and tier variants are deprecated aliases. They share
classification, tier rules and ladder history with the new names. The `redirect`
mode governs compatibility routing. Codex rejects an alias without an installed
TOML and names the canonical role to retry; rejected calls consume no round.
Spawn logs record both `asked_type` and `run_type`.

Native Claude agents (`Explore`, `Plan`, `general-purpose`) and custom agent
names pass through unchanged, including their requested model and tools. Router
does not lint their briefs, select a model, or count ladder attempts for them.
Codex has no corresponding built-in names; an unlisted Codex agent also passes
through as a custom type. Name a Router role explicitly to opt into its policy.

The writer roles have no tier files. Claude injects their ladder model directly;
Codex returns them to the owner when round three needs an upper role.

The nine base roles and base Codex TOMLs are shared byte for byte with Harness.
Both installers claim identical shared files. Uninstall keeps shared files while
the other package claims them, including when its manifest cannot be validated.

On Codex, read-only behavior and worktree isolation are requested by the role's instructions, not enforced.
As of Codex CLI 0.156, role TOMLs ignore `sandbox_mode`, `default_permissions`,
and `[permissions]`, so a role file cannot enforce a read-only sandbox. Children inherit the
parent session's sandbox. Read-only access is enforced only if the parent starts with a
restricted permission profile, using `-c default_permissions=<profile>` with a
profile defined in the user/session configuration. The `-s` flag overrides that
profile.

For a Router-only Codex judge, use the optional
[`harness-judge` profile fragment](codex/judge-permissions.toml). Merge its
`[permissions.harness-judge.*]` tables into the Codex session configuration,
preserving other tables and any existing `default_permissions`. The Router
installer does not edit that configuration or select this profile. Start a
dedicated verification session with `codex -c default_permissions=harness-judge`
from the source repository, then spawn the judge there. Do not add `-s`: it
overrides the selected profile. Set `TMPDIR` outside the repository before
starting Codex. The profile allows temporary writes, keeps workspace roots
read-only, and disables network access. If the BAR writes artifacts, run it in
a disposable copy outside every workspace root. Inspect effective host
permissions and return `SEND_BACK` if they do not protect the source or cannot
run the BAR. This fragment's TOML and standalone install are tested; host
enforcement still depends on the installed Codex version and session settings.

The route table also names light, standard and upper tiers. Claude agent frontmatter
matches that table. Codex has a TOML for every corresponding role: light sweeps use
luna/low, standard sweeps use sol/medium, and upper roles including `builder-up`
use astra/high. All judge tiers use astra/high. These are editable defaults: change
`model` and `model_reasoning_effort` in the installed role TOML, keeping its intended
tier. Add any new upper-tier or judge model name to `router.top_tier_models` in the
shared route table. Install roles in the global `~/.codex/agents/` directory (or
`$CODEX_HOME/agents/`), or register a file explicitly with
`-c agents.<name>.config_file=...`, preserving its intended tier. As of Codex CLI
0.156, project-level `.codex/agents/` may not load under `codex exec`.
Reinstallation preserves modified definitions by reporting conflicts
instead of replacing them.

The dispatch skill asks Codex to call `spawn_agent` with `agent_type`, `task_name`, `message`, and
`fork_turns="none"`. Hooks see the tool name `collaborationspawn_agent`; the template
uses matcher `.*spawn_agent`. Omit `model` and `reasoning_effort`: role TOML pins
take precedence. On both hosts, explicit Router roles receive the shared
exclusion check. Top-tier model arguments, including `gpt-6-astra` and full Opus
IDs such as `claude-opus-5-5`, require a valid visible up code for governed roles.
Native and custom types pass through with their requested model. Judges, forks,
and execution roles governed by the enforced ladder retain their existing tier
behavior. These checks obey `router.modes.block_model`;
supplying a model argument alone is not a denial. See the
[Codex instruction snippet](examples/CODEX.md.snippet).

## Ladder

The same execution brief gets Sonnet, one Sonnet resend with the judge's specific findings, then Opus. For the third attempt, add this line above the four-line brief:

```text
route: up ladder
```

A fourth attempt is blocked and goes back to the owner for replanning. The hooks count accepted execution spawns, not successful completions. They identify a brief from normalized `TASK` and `FILES` in `ledger.sqlite3` under the state directory. `router ladder list` shows keys; `router ladder reset <key>` leaves those attempts archived. The default `router.ladder_ttl_hours` is 12. Keep those lines stable during retries. `BAR`, `RETURN` and the route directive do not reset the count.

Explicit upper-tier reasons are `risk`, `security`, `novel`, `cross-cutting` and `ladder`. The execution ladder takes precedence over an early escalation request. On Claude, a judge always runs on Opus and has any resume request removed.

On Codex, preserve `task_name` across retries and use `builder` for the first two
accepted attempts, then `builder-up` for attempt three. With the ladder enforced,
the execution upper role itself is the explicit ladder request; a `route:` line inside the encrypted message has no
hook effect. An early upper role is denied with the required role to retry. A
standard role on round three and every execution role on round four are denied.
Rejected calls never consume rounds. Judges do not consume execution rounds.

Non-execution upper roles (`worker-up`, `sweeper-up`, `researcher-up`, and `planner-up`)
do not supply an up code. Both hosts reject them with `up-without-code` when
`block_model` is enforced and no valid code is visible. Codex cannot read a code
inside its encrypted message, so these roles cannot satisfy that check. Disabling
or shadowing the ladder does not bypass this independent `block_model` rule.

The shared core hashes `task_name` plus canonical `agent_type` for Codex. All tier
aliases normalize to their base role, so `builder`, `builder-std`, and
`builder-up` share history. Different base roles have separate history; do not
switch roles or rename tasks to evade the cap. History survives parent-agent and
session changes, as on Claude. Use distinct names for genuinely different tasks.

## Delegation defaults

Written delegation guidance is opt-in. Install it with
`bash install.sh --host both --with-defaults` (or select `claude` or `codex`).
Add `--dry-run` to preview changes. Without `--with-defaults`, installation never
touches your instruction files.

The block goes into `$CLAUDE_HOME/CLAUDE.md` or `$CODEX_HOME/AGENTS.md`, defaulting
to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. It describes when to delegate,
which roles to use, four-line briefs, independent judging, retries and short returns.
Codex guidance names `$dispatch`, `spawn_agent` and a stable `task_name`.
The [Claude example](examples/CLAUDE.md.snippet) and
[Codex example](examples/CODEX.md.snippet) show the default nudge wording.

Numbers are rendered from `hooks/router/routes.json`: `context.files_threshold`,
`context.command_threshold`, `context.chain.first` and return caps for each role.
The command threshold is written guidance; hooks count reads and edited files.
`router auto enforce` adds the enforcement clause. Every `router auto <mode>`
re-renders existing opted-in blocks for the known host homes using their installed routes.
If you delete the block or file, auto leaves it alone and prints a reinstall hint.
Keep custom `CLAUDE_HOME` and `CODEX_HOME` exported so both can be found.

Re-run installation with `--with-defaults` to update the managed text from the
templates. Only the range between `<!-- router:defaults:start ... -->` and
`<!-- router:defaults:end -->`, including those markers, is replaced. Outside
bytes are preserved. Appending after text without a final newline adds a separator
that uninstall removes. Existing instruction files get a `.router-backup-*` copy
before the first change, and the installer prints its path. Missing files are created
only by installation with `--with-defaults`; broken or duplicated marker
pairs stop the operation before changes. Edits inside the block are replaced.

`bash install.sh --host both --uninstall` removes installed defaults and their
markers. It deletes an instruction file only if Router created it and it becomes
empty; user text and backups remain.

## Automatic routing

`router auto` prints the current mode. The default is `nudge`; stronger routing
and prompt suggestions are opt-in. Switch with `router auto off|suggest|nudge|enforce`:

| Mode | Behavior |
| --- | --- |
| `off` | No prompt hints, chain notes or automatic read/edit blocks. Spawn rules, `large_read` and return caps keep their own modes. |
| `suggest` | The nudge behavior plus one role hint on matching prompts of at least 20 characters. Slash commands are skipped. |
| `nudge` | The existing chain note, using `context.chain` thresholds and the `chain_note` rule mode. |
| `enforce` | In the main session, blocks the next Read/Grep/Glob/Bash/Edit/Write after five consecutive read-type calls or edits to more than three distinct files since a spawn. Spawn the named role, or run `router auto nudge` to relax it. |

The thresholds are `context.chain.first` and `context.files_threshold` in the
route table. An accepted main-session spawn resets both enforcement counters;
non-read calls reset the consecutive read counter before a threshold is reached.
Enforcement counts each observed call, including calls in quick succession;
nudge retains its existing same-turn grouping and idle reset.
`context.read_allow_globs` exempts explicit matching read paths, and subagents
are exempt. Edit counting uses paths visible to PreToolUse, so a failed or
cancelled edit can still count; shell writes and edits without file paths cannot
be counted. Shell read detection uses the existing command heuristic.

Hints use the small `context.prompt_roles` keyword table; first match wins, with
docs and tests before general verbs. No match produces no hint. Hint logs contain
only prompt size and chosen role, never prompt text.
Delegation can reduce the main session's token use, but starting agents and
returning results also costs tokens.

The mode is stored in a file named `auto` beside the shared OFF switch in the
state directory (or beside `ROUTER_OFF_FILE` when overridden), leaving installed
package files unchanged. `router status` includes the mode. `router off` or
`ROUTER_OFF=1` still disables everything; `router on` preserves the auto setting.

## What each host enforces

| Check | Claude Code | Codex |
| --- | --- | --- |
| C1 spawn check | Enforced before a round is spent | not possible: spawn message is encrypted; role text asks for the brief |
| C3 lanes | Running, returned and judged | partial: running only; no SubagentStop registered, fields unverified |
| C3 verdicts, reminder, re-run block | Verdicts and one reminder, with passed brief rerun block | not possible: no verdict path |
| C4 Router note | Lanes and verdicts after compact | partial: running lanes only |
| Resume cache warning | Available through Harness | not possible: Claude-only field |
| B2 window reminders | Live window when available | partial: rollout window or policy fallback |
| C5 pressure nudges | Matching fresh pressure tightens thresholds | partial: enforce works; visibility unverified |
| run_in_background exemption | Harness permits tracked Claude tasks | n/a: no parameter; `npm run dev &` stays blocked |
| router report | Spawns, judge verdicts and opt-in persisted usage | partial: no verdicts; opt-in usage only |
| Cache-hit % | Harness statusline can show it | not possible: no command statusline |
| C7 plugins | Claude Code marketplace route above | not shipped |

Codex role text requests a fresh judge and four fields, but its hook cannot read the encrypted spawn brief. Its compact note lists running lanes only. Codex patch edit visibility and pressure nudge display remain unverified.

A lane is one task's worktree run. `close-lane.sh` checks its brief for exactly one nonempty TASK, FILES, BAR, and
RETURN on either host. It accepts free-text notes after those fields. The dispatch
skill and Codex roles validate the four fields before work. The hook cannot verify
that the encrypted message follows them. Both adapters use the routing and ladder
implementation in `hooks/router/common.py`; Codex does not duplicate that policy.

## Kill switch

```bash
router off
router status
router on
```

`router off` creates the persistent off file. Every router hook on both hosts then exits without rewriting or blocking the tool call. `router on` removes the file. Setting `ROUTER_OFF=1` also disables all router hooks; unset it to enable them again. These switches do not cancel agents that are already running.

## Configuration

Set overrides in the environment inherited by your host and by the CLI.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_HOME` | `$HOME/.claude` | Claude install location and agent lookup root |
| `CODEX_HOME` | `$HOME/.codex` | Codex install location and role lookup root |
| `ROUTER_HOME` | Host installation support directory | Explicit router support location for fallback lookup |
| `ROUTES_JSON` | `hooks/router/routes.json` beside the installed hook | Alternate route table |
| `ROUTER_STATE` | `$XDG_STATE_HOME/claude-router`, or `$HOME/.local/state/claude-router` | Attempt database, logs and context state |
| `XDG_STATE_HOME` | `$HOME/.local/state` | Base state directory when `ROUTER_STATE` is unset |
| `ROUTER_OFF_FILE` | `$ROUTER_STATE/OFF` | Alternate persistent kill-switch file |
| `ROUTER_OFF` | Unset | `1` disables routing immediately |
| `ROUTER_LOCAL` | `${XDG_CONFIG_HOME:-$HOME/.config}/router/routes.local.json` | Local JSON Merge Patch overlay; `off` disables it |
| `ROUTER_TEST_NOW_MS` | System clock | Test-only context-hook time, in milliseconds since the Unix epoch; leave unset for normal use |
| `ROUTER_PYTHON` | Unset | Python for hooks and the `router` command, used before the saved interpreter |

`ROUTER_STATE` or `XDG_STATE_HOME` written as `~user/...` needs `ROUTER_PYTHON` or a Python 3.9 or newer on PATH to resolve that account. On native Windows, a `~/` path whose rest starts with another separator or a drive, such as `~//tmp` or `~/C:state`, may need one too. If the saved interpreter is reachable only through such a path and no Python is found, the missing-Python line applies. Absolute paths, drive and UNC paths, and ordinary `~/` paths start from the saved interpreter.

The hooks write `ledger.sqlite3` under the state directory with file mode 0600 and directory mode 0700. It stores no prompt, BAR, RETURN or finding text; verdict findings are counts. They do not log prompt bodies. Context limits are character counts configured in `context.caps`. Read-only agents trim their replies instead of writing reports. Internal hook errors allow the tool call and record an error when possible.

## 0.3 commands

| Command | Use |
| --- | --- |
| `router ladder list\|reset` | List ladder keys or reset a key, which is archived. |
| `router lanes` | Show recent lanes of this project; `--all` shows every project; `--session HASH` filters one session. |
| `router checklist` | Print the three completion evidence steps: inspection, acceptance review, and external readback. This is guidance; it does not run or record those checks. |
| `router report` | Show spawns, judge coverage, first pass rates, context blocks and all-time opt-in persisted usage; `--since N` sets the routing window (such as `36h` or `7d`, default 7d, at most 90d) and `--json` prints the data as JSON. |
| `router operations check --file PATH` | Validate an opt-in operations brief and exact declared tools before dispatch. Missing tools exit 3; unknown visibility remains unknown. `--file -` reads stdin. See [operations](docs/operations.md). |
| `router observe --file PATH` | Compare supplied expected proof and independent readback without repair, replay or automatic gating. Valid PASS/FAIL/UNKNOWN reports exit 0. `--file -` reads stdin. |
| `router evaluate --file PATH` | Score supplied paired experiment records and independent check references offline. Missing usage prevents an efficiency claim; no automatic adoption. `--file -` reads stdin. See [evaluation](docs/evaluation.md). |
| `router usage declare TASK_ID --file PATH` / `router usage record TASK_ID --file PATH` / `router usage report [TASK_ID]` | Declare expected observations, record one normalized host observation, or read all-time usage. `--file -` reads stdin. |
| `router doctor` | Check installed hooks, roles, routes, versions and sibling skew without changing files; `--host claude` or `--host codex` checks one host. `--host-version VERSION` requires one explicit `--host`. |
| `router config path\|get\|set\|unset\|check\|migrate inheritance [--apply]` | Find, inspect, change or validate the local override; preview legacy unlisted fallback cleanup, or use `--apply` to save an exact private backup and change only its mode. |
| `router status --agent NAME` | Show the requested and canonical agent type, whether Router governs it, and host limits. |
| `router promote RULE --dry-run` | Preview promotion from shadow to enforce; `--since N` sets the evidence window (at most 90d); without dry run, report evidence is required unless `--force` is given. |
| `router receipt create TASK_ID --file PATH` / `router receipt record TASK_ID --file PATH` / `router receipt show TASK_ID` | Explicitly create, record or show a local receipt. `--file -` reads a JSON object from stdin. Recording never accepts work automatically. |

The local override path is `${XDG_CONFIG_HOME:-$HOME/.config}/router/routes.local.json`; set `ROUTER_LOCAL=PATH` to use another file. Apply [careful](examples/presets/careful.local.json), [medium](examples/presets/medium.local.json), or [small](examples/presets/small.local.json) by pointing `ROUTER_LOCAL` at it or copying it to the override path. `router config set` and `router promote` replace a symlinked overlay with a real 0600 file; the symlink target is untouched. An invalid override falls back to shipped routes for hooks and is reported by status, config check and doctor.

The completion checklist is an explicit command. Inspect the changed files or diff; compare requirements with BAR evidence and record who performed the acceptance review; then observe any external state without repeating the mutation, recording evidence, not-applicable, or unknown. A reported PASS or recorded lane verdict does not establish independent acceptance. Router's Stop reminder requests acceptance review for returned lanes without a recorded review. Its compact restore note reports lane status without claiming acceptance. When Harness is installed too, its own Stop hook requests inspection after an indirect edit with no later successful read or diff. Each hook acts on its own condition; either can prompt, both can prompt, or both can remain silent. A successful inspection clears only the Harness inspection reminder, and a recorded lane review clears only Router's returned-lane reminder. Neither hook performs external readback or prints the full checklist automatically.

Named native and custom agents already pass through without Router changing their requested model, whether it is absent, `inherit`, a pin in the agent definition, or an explicit model. Forks inherit from their parent. The old unlisted injection rule remains a fallback for unresolved types. `router config migrate inheritance` previews setting that legacy rule's mode to `off`; only `--apply` writes an override. It keeps model maps, pins, aliases and other settings, and refuses invalid or symlinked overrides. A repeat after cleanup makes no change.

`router.modes.brief`, `router.modes.rerun` and `router.modes.judge_reminder` control brief checks, a same-session rerun block and judge reminders. `router.lane_labels` controls saved lane labels. `router.ladder_ttl_hours` controls the ladder window. Hint settings `context.prompt_roles` and `context.hint_every` tune scored hints and their rate limit; `router auto` selects off, suggest, nudge or enforce. Harness context-pressure nudges tighten read and chain thresholds when a fresh matching signal exists.

Router picks the pressure host from `harness_host` first, then an explicit `--host` argument, and only then treats an event that carries `turn_id` as Codex; live Codex visibility of pressure nudges remains unverified.

`router doctor` appends a static capability catalog for each selected host. With no `--host-version`, the host version is unknown. Supplying a version records caller input only; an exact match to the reference Claude 2.1.296 or Codex 0.156.1 version still leaves host capabilities unverified. Those reference versions came from version output, not capability tests. The catalog identifies adapter fixtures and fields consumed by source; it does not prove that a host supplies those fields, trusts hooks, enforces read-only roles, or supports live usage collection. Its suggested checks are manual and disposable; doctor performs none of them.

## Usage accounting

Usage accounting is an explicit, offline import. `router usage declare TASK_ID --file PATH` accepts a JSON declaration, `router usage record TASK_ID --file PATH` accepts one JSON observation, and `router usage report [TASK_ID]` reads saved observations. `PATH` can be `-` for stdin. Inputs must be UTF-8 JSON objects of at most 64 KiB; file inputs must be regular files. All three commands print sorted JSON. Reports alone create no state; declarations and observations live in a separate private `usage/usage.sqlite3` under `ROUTER_STATE`.

Use opaque task, event, attempt and stream IDs (1–64 ASCII letters, digits, `_` or `-`), never paths or raw prompt text. An example declaration for `router usage declare demo_task --file declaration.json` is:

```json
{"host":"claude","expected_event_ids":["event_1"],"outcome":"returned"}
```

An example observation for `router usage record demo_task --file observation.json` is:

```json
{"adapter":"claude-message","context":{"event_id":"event_1","attempt_id":"attempt_1","stage":"worker","outcome":"returned"},"payload":{"type":"assistant","message":{"usage":{"input_tokens":10,"output_tokens":5}}}}
```

Supported adapters are `claude-message`, `codex-turn`, `codex-cumulative` and `missing`. Use a stable host event ID or export ordinal for `event_id`, and submit finalized observations only; the tool cannot authenticate identity or infer one from token counts. `missing` takes an empty payload `{}` and records unavailable telemetry. Codex cumulative observations need a stable `stream_id` and increasing `sequence`; only an explicit sequence-zero `from_start` establishes complete prefix coverage. Declare the complete expected event ID inventory where known, or use `null` to mark it unknown. Unknown inventory, absent fields, gaps and missing attribution stay incomplete; a missing value is never treated as zero. Claude cache components are disjoint; Codex cache and reasoning counters are subsets and are not counted twice.

`router report --since` still filters routing history only. Its usage section and `router usage report` show all-time persisted observations, including failed and abandoned attempts and judge events. The old default Claude transcript sampler has been retired; no command automatically scans private transcripts or Codex rollouts. Existing occupancy/statusline cost displays and bytes/4 budgets are separate. Usage reports have no independently verified acceptance, token efficiency or observed cost: acceptance is `unverified`, its denominator is zero, and those derived values remain N/A. These adapters describe deterministic host formats; live hook collection has not been verified.

## Receipts

Receipts are an explicit opt-in local record of a coordinator's claims. `router receipt create TASK_ID --file PATH` creates a new task, `router receipt record TASK_ID --file PATH` adds one event, and `router receipt show TASK_ID` reads the current snapshot. `--file -` reads stdin. The file path locates input only and is not stored in the receipt. There are no automatic hook writes, no automatic acceptance, and no live-host or usage claims from this command. Receipt data stays in Router's private local state directory; keep access to that directory private.

Use opaque identifiers for tasks, events, attempts, actors, verifiers, requirements and evidence: 1–64 ASCII letters, digits, `_` or `-`. Do not use paths, URLs, hostnames or free text. `alignment_digest` is a 64-character lowercase hex digest. An optional `brief_id` is a 16- or 64-character lowercase hex digest. Artifact `revision` is a 40- or 64-character lowercase hex digest and `readback` is a 64-character lowercase hex digest; they identify content, not locations. Input must be a JSON object and is limited to 64 KiB. These synthetic examples can be saved as `create.json`, `running.json`, `returned.json` and `attestation.json`, then submitted in that order with the commands above:

`create.json` for `router receipt create task_demo --file create.json`:

```json
{"alignment_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","requirements":["R1"],"artifact":{"revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}
```

`running.json` for `router receipt record task_demo --file running.json`:

```json
{"event_id":"event_1","kind":"attempt","attempt_id":"attempt_1","actor_id":"worker_1","revision":1,"state":"running"}
```

`returned.json` for `router receipt record task_demo --file returned.json`:

```json
{"event_id":"event_2","kind":"attempt","attempt_id":"attempt_1","actor_id":"worker_1","revision":1,"state":"returned"}
```

`attestation.json` for `router receipt record task_demo --file attestation.json`:

```json
{"event_id":"event_3","kind":"attestation","attempt_id":"attempt_1","verifier_id":"reviewer_1","revision":1,"artifact":{"revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"verdict":"PASS","evidence":{"R1":["evidence_1"]},"provenance":"coordinator-recorded"}
```

`returned` records an attempt's state. An attestation records a submitted verdict and opaque evidence references. The snapshot's `attestation_summary` may show a current coordinator-recorded PASS, but `verification` remains `unverified` with verdict `UNKNOWN` and provenance `unavailable`. `host-observed` provenance is reserved and rejected by this recording API. No submitted attestation establishes independent verification or acceptance.

## Adapt the routes

Edit a copy of [routes.json](hooks/router/routes.json), then set `ROUTES_JSON` to that file. This preserves local policy across reinstalls.

- `tiers` maps each base agent and tier to an agent file, model and effort. Keep the matching installed agent's frontmatter in sync.
- Top-tier model checks use every `tiers.*.up.model` plus `router.top_tier_models`, a shared list of additional model names (initially `gpt-6-astra`, matching the upper and judge TOML pins). Keep this list in sync with edited TOML pins on both hosts. Names ignore case and surrounding whitespace; full Claude model IDs normalize to their family.
- `router.types` classifies Router seats. Preserve execution and judge classes if you want their ladder and fresh-context rules. Routing decisions use the class and tier.
- Each rule in `router.modes` and `context.modes` accepts `off`, `shadow` or `enforce`. Off skips the rule. Shadow records what would happen without applying it. Enforce applies it. Both spawn adapters obey the same router mode keys, subject to the host differences above. Other enforced rules still apply when one rule is disabled. The global kill switch disables all of them.
- `risk_globs`, `risk_words`, `up_codes` and `context.caps` tune scope signals, escalation reasons and return limits.

Run `router status` to load and validate the selected table and show its modes. Run the repository tests after changing routing policy or agent definitions. The default risk rule is in shadow mode; the execution ladder is enforced.

Generate tier Markdown and Codex TOMLs with `python3 scripts/generate_roles.py`;
use `--check` to detect drift. Tier bodies come directly from the shared base role.
`tiers` selects Claude model and effort; `codex_models` maps those models to Codex
model and effort pins. `agents/SHARED.sha256` verifies the nine shared sources.

## Helpers

The installer places these alongside `router` in `router/bin`:

| Helper | Use |
| --- | --- |
| `wait-until.sh SECONDS exists FILE` | Wait for a file, with exit code 124 on timeout |
| `wait-until.sh SECONDS line FILE REGEX` | Wait for a matching line |
| `wait-until.sh SECONDS pid PID...` | Wait for processes to exit |
| `wait-until.sh SECONDS no-proc REGEX` | Wait until no process matches |
| `cite-check.py REPORT --root WORKTREE` | Check file and line citations in a report |
| `close-lane.sh WORKTREE BRIEF` | Check changed paths against `FILES`, run `BAR`, then check scope again |

On native Windows, `wait-until.sh` checks a `pid` without signalling it, reads the process table itself for `no-proc`, and maps a pid from Git Bash `$!` to its Windows pid.

For Windows `no-proc`, readable command lines are matched in full. Inaccessible processes and unreadable processes of other accounts are matched by executable name only. A failed process-table read or an unreadable command line of a live process owned by your account exits 2; it does not report successful absence.

`close-lane.sh` runs the BAR from the worktree root. Give it a brief you trust: the BAR is a shell command. In `FILES`, `*` and `?` match within one path segment, `**` spans segments, and a trailing `/` allows a subtree. Quote patterns containing spaces.

Use `CLOSE_LANE_BASE` to choose the Git base for committed changes; the default is local `main`, then `master`, then `HEAD`. The check freezes the merge base before running the BAR. With the `HEAD` fallback, only working changes are checked, so specify a base when reviewing commits on another branch. `CLOSE_LANE_BAR_TIMEOUT` limits the check, defaulting to 900 seconds. Neither a scope check nor a passing BAR replaces the fresh judge.

The verdict record is the ledger. Use `router lanes` and `router report` to inspect it.

## Operations and observation

Version 0.4.0 includes these opt-in features. An operations brief describes a service
target, allowed action, proof and readback without requiring a Git worktree. The preflight reports declared missing
tools before dispatch; it preserves the user's named agent and its configuration.
A separate report-only observer compares supplied resulting-state proof and never
replays or repairs a mutation. See [operations and observation](docs/operations.md)
for the exact profile, commands, coordinator recipe and enforcement boundaries.

## Offline evaluation

The [offline evaluator](docs/evaluation.md) checks supplied paired experiment
records against a frozen rubric and control manifest. It retains failed attempts,
missing usage and independent-check evidence. Its result is conservative screening,
not automatic acceptance, live-host verification or an optimization claim.

## Development

Tests need no network, private configuration or model calls. `tests/run_all.py` runs every test file with a per-file timeout and lists every failure; in Git Bash on Windows, run it with `python`:

<!-- docs:skip -->
```bash
python3 tests/run_all.py
```

Native Windows test runs skip POSIX owner and mode assertions and chmod-based unreadable-file fixtures; checks of file contents, copy lifecycles and injected errors still run. Where the account cannot create symlinks, symlink assertions are skipped and the copy fallback stays tested. FIFO fixtures need `os.mkfifo`, which Windows Python lacks. The system-Bash release-notes check runs only when `/bin/bash` is older than 4. The Windows simulation self-check runs only under `PYTHONPATH=tests/win_sim`. Tests that start the 0.1 or 0.2 installers need Python 3.10 or newer (a stock Mac has 3.9) and skip only those cases when none is on PATH; on native Windows, the cases where an old 0.2 uninstaller runs first are skipped.

Both fresh-user tests install into temporary homes and exercise the installed hooks and shared kill switch. The Codex test sets temporary HOME and CODEX_HOME; it never starts Codex or calls a model. The package includes a Gitleaks configuration that extends its default secret rules.
To check installation alongside Harness in both orders, run `HARNESS_DIR=/path/to/harness bash tests/together.sh`.

Released under the [MIT license](LICENSE). Copyright (c) 2026 AEIA.
