# Router

Get cheaper models for simple work, stronger models for hard work, and every result judged. A brief is checked before it costs a run, and active lanes are restored after compaction.

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

Split work only when it divides cleanly, fits a clear four-line brief, has a verifier and saves more effort than delegation costs.

1. The owner writes `TASK`, `FILES`, `BAR` and `RETURN`.
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

## Install

Pair with [Harness](https://github.com/aeiadev/harness) for guarded commands and context restore; the shared Install both and Upgrade block above covers both tools.

Requirements: Bash, Python 3.10 or newer, Git and Claude Code or Codex CLI with subagents and command hooks. The Codex adapter uses the spawn hook format as of Codex CLI 0.156. Use a Unix-like environment. The hook state uses standard-library SQLite and file locking. The helper checks and test commands use `timeout` from GNU coreutils.

From a local checkout:

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
| `general-purpose` | `worker` |
| `Explore` | `researcher` |
| `Plan` | `planner` |

Old names and their existing tier variants are deprecated aliases until a later
release. They share classification, tier rules and ladder history with the new
names. `worker-std` and `worker-up` keep their names and tiers. The `redirect`
mode governs compatibility routing. Codex rejects an alias without an installed
TOML and names the canonical role to retry; rejected calls consume no round.
Spawn logs record both `asked_type` and `run_type`.

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
take precedence. On both hosts, explicit model arguments receive the shared
exclusion check. Top-tier model arguments, including `gpt-6-astra` and full Opus
IDs such as `claude-opus-5-5`, require a valid visible up code even on unlisted
roles. Judges, forks, and execution roles governed by the enforced ladder retain
their existing tier behavior. These checks obey `router.modes.block_model`;
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
| router report | Spawns, judge verdicts and readable tokens | partial: no verdicts; readable tokens only |
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

The hooks write `ledger.sqlite3` under the state directory with file mode 0600 and directory mode 0700. It stores no prompt, BAR, RETURN or finding text; verdict findings are counts. They do not log prompt bodies. Context limits are character counts configured in `context.caps`. Read-only agents trim their replies instead of writing reports. Internal hook errors allow the tool call and record an error when possible.

## 0.3 commands

| Command | Use |
| --- | --- |
| `router ladder list\|reset` | List ladder keys or reset a key, which is archived. |
| `router lanes` | Show recent lanes of this project; `--all` shows every project; `--session HASH` filters one session. |
| `router report` | Show spawns, judge coverage, first pass rates, context blocks and readable token totals; `--since N` sets the window (such as `36h` or `7d`, default 7d, at most 90d) and `--json` prints the data as JSON. |
| `router doctor` | Check installed hooks, roles, routes, versions and sibling skew without changing files; `--host claude` or `--host codex` checks one host. |
| `router config path\|get\|set\|unset\|check` | Find, inspect, change or validate the local override. |
| `router promote RULE --dry-run` | Preview promotion from shadow to enforce; `--since N` sets the evidence window (at most 90d); without dry run, report evidence is required unless `--force` is given. |

The local override path is `${XDG_CONFIG_HOME:-$HOME/.config}/router/routes.local.json`; set `ROUTER_LOCAL=PATH` to use another file. Apply [careful](examples/presets/careful.local.json), [medium](examples/presets/medium.local.json), or [small](examples/presets/small.local.json) by pointing `ROUTER_LOCAL` at it or copying it to the override path. `router config set` and `router promote` replace a symlinked overlay with a real 0600 file; the symlink target is untouched. An invalid override falls back to shipped routes for hooks and is reported by status, config check and doctor.

`router.modes.brief`, `router.modes.rerun` and `router.modes.judge_reminder` control brief checks, a same-session rerun block and judge reminders. `router.lane_labels` controls saved lane labels. `router.ladder_ttl_hours` controls the ladder window. Hint settings `context.prompt_roles` and `context.hint_every` tune scored hints and their rate limit; `router auto` selects off, suggest, nudge or enforce. Harness context-pressure nudges tighten read and chain thresholds when a fresh matching signal exists. An event is treated as Codex pressure only when it carries `turn_id`; this detection is inferred and unverified against a live Codex session.

## Adapt the routes

Edit a copy of [routes.json](hooks/router/routes.json), then set `ROUTES_JSON` to that file. This preserves local policy across reinstalls.

- `tiers` maps each base agent and tier to an agent file, model and effort. Keep the matching installed agent's frontmatter in sync.
- Top-tier model checks use every `tiers.*.up.model` plus `router.top_tier_models`, a shared list of additional model names (initially `gpt-6-astra`, matching the upper and judge TOML pins). Keep this list in sync with edited TOML pins on both hosts. Names ignore case and surrounding whitespace; full Claude model IDs normalize to their family.
- `router.types` classifies seats and built-in names. Preserve execution and judge classes if you want their ladder and fresh-context rules. Routing decisions use the class and tier.
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

`close-lane.sh` runs the BAR from the worktree root. Give it a brief you trust: the BAR is a shell command. In `FILES`, `*` and `?` match within one path segment, `**` spans segments, and a trailing `/` allows a subtree. Quote patterns containing spaces.

Use `CLOSE_LANE_BASE` to choose the Git base for committed changes; the default is local `main`, then `master`, then `HEAD`. The check freezes the merge base before running the BAR. With the `HEAD` fallback, only working changes are checked, so specify a base when reviewing commits on another branch. `CLOSE_LANE_BAR_TIMEOUT` limits the check, defaulting to 900 seconds. Neither a scope check nor a passing BAR replaces the fresh judge.

The verdict record is the ledger. Use `router lanes` and `router report` to inspect it.

## Development

Tests need no network, private configuration or model calls:

<!-- docs:skip -->
```bash
timeout 900 bash -c 'for t in tests/test_*.py; do python3 "$t" || exit 1; done && bash tests/fresh-user.sh && bash tests/fresh-user-codex.sh'
```

Both fresh-user tests install into temporary homes and exercise the installed hooks and shared kill switch. The Codex test sets temporary HOME and CODEX_HOME; it never starts Codex or calls a model. The package includes a Gitleaks configuration that extends its default secret rules.
To check installation alongside Harness in both orders, run `HARNESS_DIR=/path/to/harness bash tests/together.sh`.

Released under the [MIT license](LICENSE). Copyright (c) 2026 AEIA.
