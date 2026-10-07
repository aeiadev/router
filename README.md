# Router

Router sends bounded Claude Code and OpenAI Codex CLI tasks to the model that fits them and gives each result to a fresh judge. It combines a dispatch skill, named agents, routing hooks and small command-line helpers. Python code uses only the standard library.

For developers using Claude Code or Codex CLI who split work across subagents and want cheap models on simple tasks, stronger ones on hard tasks, and every result checked.

A seat is a named agent role. The owner is the session coordinating the work. Each task has a BAR: the acceptance command that checks its result.

## How a brief flows

Split work only when it divides cleanly, fits a clear four-line brief, has a verifier and saves more effort than delegation costs.

1. The owner writes `TASK`, `FILES`, `BAR` and `RETURN`.
2. Haiku handles a bounded read-only sweep. Sonnet builds in its own Git worktree.
3. The worker returns five lines: `CHANGED`, `BAR`, `OUTPUT`, `NOT DONE`, `OPEN`.
4. A fresh Opus judge reads the brief and result, inspects the files and runs the BAR itself. Its verdict is `PASS` or `SEND_BACK`. It never fixes the work.
5. The owner accepts a verified result or retries with the concrete failure. The attempt ladder allows two standard attempts, then one upper-tier attempt.

The dispatch skill guides this workflow. The host-specific hooks enforce the checks listed below; the owner still starts the judge and decides whether to accept the result.

## Install

Requirements: Bash, Python 3.10 or newer, Git and Claude Code or Codex CLI with subagents and command hooks. The Codex adapter uses the spawn hook format as of Codex CLI 0.156. Use a Unix-like environment. The hook state uses standard-library SQLite and file locking. The helper checks and test commands use `timeout` from GNU coreutils.

From a local checkout:

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

```bash
export CLAUDE_HOME="$HOME/.claude-test"
bash install.sh
export PATH="$CLAUDE_HOME/router/bin:$PATH"
```

Review [the settings example](examples/settings.example.json) for the hooks block and add [the short instruction snippet](examples/CLAUDE.md.snippet) to your own `CLAUDE.md` if useful. The installer does not edit `CLAUDE.md`. Start a new Claude Code session after installation so the agents and skill are loaded.

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

Use paths relative to the repository root. Keep `FILES` narrow and make `BAR` a real command that proves the task. The worker runs it, then a fresh `seat-judge` runs it again in the result's worktree. A sweep can use the same format with read-only files and a check that verifies its answer.

For example, send this brief to Claude Code with `/dispatch`:

```text
TASK Check the parser's empty-input behavior.
FILES src/parser.py tests/test_parser.py
BAR timeout 60 python3 tests/test_parser.py
RETURN Five lines: CHANGED / BAR / OUTPUT / NOT DONE / OPEN.
```

In Codex, send the same brief after invoking `$dispatch`. This simple check routes to the sweep seat: Claude Code's hook rewrites `seat-sweep` to `seat-sweep-light` (Haiku); Codex keeps `seat-sweep` (gpt-6-luna, low). Each decision is logged with `run_type` and `tier` in `~/.local/state/claude-router/spawns.jsonl` by default (`ROUTER_STATE` or `XDG_STATE_HOME` moves it).

| Seat | Claude default | Codex default | Job |
| --- | --- | --- | --- |
| `seat-sweep` | Haiku | gpt-6-luna, low | Bounded searches, read-only |
| `seat-exec` | Sonnet | gpt-6-sol, medium | Build in an isolated worktree |
| `seat-exec-here` | Sonnet | gpt-6-sol, medium | Execute in an assigned directory |
| `seat-judge` | Opus | gpt-6-astra, high | Fresh verification, read-only |
| `Explore`, `Plan`, `general-purpose` | Sonnet | gpt-6-sol, medium | Research, planning, bounded work |

On Codex, read-only behavior and worktree isolation are requested by the role's instructions, not enforced.
As of Codex CLI 0.156, role TOMLs ignore `sandbox_mode`, `default_permissions`,
and `[permissions]`, so a role file cannot enforce a read-only sandbox. Children inherit the
parent session's sandbox. Read-only access is enforced only if the parent starts with a
restricted permission profile, using `-c default_permissions=<profile>` with a
profile defined in the user/session configuration. The `-s` flag overrides that
profile.

The route table also names light, standard and upper tiers. Claude agent frontmatter
matches that table. Codex has a TOML for every corresponding role: light sweeps use
luna/low, standard sweeps use sol/medium, and upper roles including `seat-exec-up`
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

A fourth attempt is blocked and goes back to the owner for replanning. The hooks count accepted execution spawns, not successful completions. They identify a brief from normalized `TASK` and `FILES` and store the count in `attempts.sqlite3` under the state directory. Keep those lines stable during retries. `BAR`, `RETURN` and the route directive do not reset the count.

Explicit upper-tier reasons are `risk`, `security`, `novel`, `cross-cutting` and `ladder`. The execution ladder takes precedence over an early escalation request. On Claude, a judge always runs on Opus and has any resume request removed.

On Codex, preserve `task_name` across retries and use `seat-exec` for the first two
accepted attempts, then `seat-exec-up` for attempt three. With the ladder enforced,
the execution upper role itself is the explicit ladder request; a `route:` line inside the encrypted message has no
hook effect. An early upper role is denied with the required role to retry. A
standard role on round three and every execution role on round four are denied.
Rejected calls never consume rounds. Judges do not consume execution rounds.

Non-execution upper roles (`worker-up`, `seat-sweep-up`, `explore-up`, and `plan-up`)
do not supply an up code. Both hosts reject them with `up-without-code` when
`block_model` is enforced and no valid code is visible. Codex cannot read a code
inside its encrypted message, so these roles cannot satisfy that check. Disabling
or shadowing the ladder does not bypass this independent `block_model` rule.

The shared core hashes `task_name` plus canonical `agent_type` for Codex. All tier
aliases normalize to their base role, so `seat-exec`, `seat-exec-std`, and
`seat-exec-up` share history. Different base roles have separate history; do not
switch roles or rename tasks to evade the cap. History survives parent-agent and
session changes, as on Claude. Use distinct names for genuinely different tasks.

## What each host enforces

| Check | Claude hooks | Codex hooks |
| --- | --- | --- |
| Rule modes | `off` skips, `shadow` records without applying, `enforce` applies | Same `router.modes` keys and shared allow/deny policy for visible inputs |
| Model selection | Route rewrites and agent frontmatter | Installed role TOML pins win over per-call model/effort; shared model exclusion and up-code checks apply |
| Unlisted or omitted role | Shared model checks apply; model injection or default-role redirect follows its rule mode | Shared model checks apply; no model injection or default-role rewrite. Name an installed role to obtain its pins |
| Execution ladder | TASK/FILES hash, two standard attempts then explicit upper, fourth denied | Task name/base-role hash, same shared policy and transactional counter; a missing task name is denied only with `ladder=enforce` |
| Incompatible execution tier | Rewrites the role/model to the required tier | Denies with the role to retry when the ladder is enforced, because a pinned model cannot be rewritten |
| Explicit up code | Reads `route: up <code>` from the prompt | Cannot read encrypted route text; only an execution upper role with an enforced ladder supplies the ladder request |
| Nested spawns | Guarded | Guarded |
| Kill switch | Shared OFF file or `ROUTER_OFF=1` | The same file and environment flag |
| Brief risk words and FILES globs | Available; risk mode defaults to shadow | Unavailable because `message` is encrypted |
| Four-field brief lint | Skill and agent instructions; not spawn-hook enforced | Role `developer_instructions` and dispatch skill; not hook-enforced |
| Fresh judge context | Resume removed by spawn hook | Requested by role and skill instructions (`fork_turns="none"`); not hook-enforced |
| Return size and reading/context rules | Context hook, according to rule modes | Role instructions only; no Codex context hook installed |

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
| `ROUTER_TEST_NOW_MS` | System clock | Test-only context-hook time, in milliseconds since the Unix epoch; leave unset for normal use |

The hooks write diagnostic metadata under the state directory. They do not log prompt bodies. Context limits are character counts configured in `context.caps`. Read-only agents trim their replies instead of writing reports. Internal hook errors allow the tool call and record an error when possible.

## Adapt the routes

Edit a copy of [routes.json](hooks/router/routes.json), then set `ROUTES_JSON` to that file. This preserves local policy across reinstalls.

- `tiers` maps each base agent and tier to an agent file, model and effort. Keep the matching installed agent's frontmatter in sync.
- Top-tier model checks use every `tiers.*.up.model` plus `router.top_tier_models`, a shared list of additional model names (initially `gpt-6-astra`, matching the upper and judge TOML pins). Keep this list in sync with edited TOML pins on both hosts. Names ignore case and surrounding whitespace; full Claude model IDs normalize to their family.
- `router.types` classifies seats and built-in names. Preserve execution and judge classes if you want their ladder and fresh-context rules. Routing decisions use the class and tier.
- Each rule in `router.modes` and `context.modes` accepts `off`, `shadow` or `enforce`. Off skips the rule. Shadow records what would happen without applying it. Enforce applies it. Both spawn adapters obey the same router mode keys, subject to the host differences above. Other enforced rules still apply when one rule is disabled. The global kill switch disables all of them.
- `risk_globs`, `risk_words`, `up_codes` and `context.caps` tune scope signals, escalation reasons and return limits.

Run `router status` to load and validate the selected table and show its modes. Run the repository tests after changing routing policy or agent definitions. The default risk rule is in shadow mode; the execution ladder is enforced.

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
| `dispatch-log.py list` | Read the optional generic run log |

`close-lane.sh` runs the BAR from the worktree root. Give it a brief you trust: the BAR is a shell command. In `FILES`, `*` and `?` match within one path segment, `**` spans segments, and a trailing `/` allows a subtree. Quote patterns containing spaces.

Use `CLOSE_LANE_BASE` to choose the Git base for committed changes; the default is local `main`, then `master`, then `HEAD`. The check freezes the merge base before running the BAR. With the `HEAD` fallback, only working changes are checked, so specify a base when reviewing commits on another branch. `CLOSE_LANE_BAR_TIMEOUT` limits the check, defaulting to 900 seconds. Neither a scope check nor a passing BAR replaces the fresh judge.

The optional logger writes `$ROUTER_STATE/runs.jsonl` with the same state default as the hooks. For example:

```bash
dispatch-log.py add --family parser-empty --seat seat-exec --model sonnet --verdict PASS
dispatch-log.py rounds parser-empty
```

For each family it caps execution records at two Sonnet rounds followed by one Opus round marked `--escalated`. It is a separate run record; hook enforcement uses `attempts.sqlite3`.

## Development

Tests need no network, private configuration or model calls:

```bash
timeout 900 bash -c 'for t in tests/test_*.py; do python3 "$t" || exit 1; done && bash tests/fresh-user.sh && bash tests/fresh-user-codex.sh'
```

Both fresh-user tests install into temporary homes and exercise the installed hooks and shared kill switch. The Codex test sets temporary HOME and CODEX_HOME; it never starts Codex or calls a model. The package includes a Gitleaks configuration that extends its default secret rules.

Released under the [MIT license](LICENSE). Copyright (c) 2026 AEIA.
