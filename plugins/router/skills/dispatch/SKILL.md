---
name: dispatch
description: Split work into four-line briefs, route each to a seat, verify each return with a fresh judge. Use for bounded parallel work, evidence sweeps, independent verification.
---

# Dispatch

The owner holds the goal, writes briefs, and integrates verified results. Seats do
bounded work with only the context their brief needs.

See "Delegation defaults" in the Router checkout's README.md for opt-in written guidance with route-derived thresholds.

## Four split tests

Dispatch a piece only when all four answers are yes:

1. **Splits cleanly:** it can finish independently of the other pieces. Schedule
   dependent steps in order instead of running them in parallel.
2. **Clean four-line brief:** a new worker can act from TASK, FILES, BAR, and RETURN
   without the conversation that produced them.
3. **A verifier exists:** someone can inspect the result and independently run the
   BAR. A fresh judge will check every returned result.
4. **Worth the cost:** the saved time or independent evidence justifies the extra
   model calls and context. Keep a small direct question in the owner session.

## Four-line brief

```text
TASK    One bounded job with a clear outcome.
FILES   The only paths or globs the seat may inspect or change.
BAR     The exact runnable check that proves the outcome.
RETURN  Five lines: CHANGED / BAR / OUTPUT / NOT DONE / OPEN.
```

Before dispatching, check that the brief has exactly one nonempty TASK, FILES, BAR,
and RETURN field, in that order. Free-text notes may follow the four fields.
Reject incomplete or duplicate fields before starting work.

Name the repository or worktree separately so relative FILES paths are unambiguous.
Use pointers to existing code or a specification instead of copying large files.
Keep independent execution seats in separate worktrees and give each exclusive
file ownership. Preserve the original TASK and FILES on a resend. The Claude hook groups
execution attempts by those two fields; renaming them to reset a round is not a fix.

A worker returns exactly these five lines:

```text
CHANGED: paths and the change, or none for a read-only result.
BAR: the exact check run.
OUTPUT: PASS or FAIL and concise evidence.
NOT DONE: incomplete or excluded work, or none.
OPEN: one unresolved question, or none.
```

## Choose a seat

| Work | Seat | Default model |
| --- | --- | --- |
| Find, list, count, or summarize evidence | `sweeper` | Haiku |
| Implement one bounded change in its own worktree | `builder` | Sonnet |
| Execute in an explicitly assigned directory | `builder-in-place` | Sonnet |
| Inspect every return independently, in fresh context | `judge` | Opus |
| Research code for a decision | `researcher` | Sonnet |
| Propose an implementation plan | `planner` | Sonnet |
| Complete other bounded research or implementation | `worker` | Sonnet |
| Write focused failing tests | `test-writer` | Sonnet |
| Update named documentation | `docs-writer` | Sonnet |

`builder` declares `isolation: worktree` on Claude. `builder-in-place` uses an
explicitly assigned directory. On Codex, create or use a separately assigned
worktree before dispatch; there is no Claude isolation argument. Never give
simultaneous editing seats the same checkout.

On Claude, request the base agent name and let the router select the tier. Do not set model
or effort by hand. A standalone `route: light` line requests a lighter tier where
available. A standalone `route: up <code>` line requests an up tier; valid codes are
`risk`, `security`, `novel`, `cross-cutting`, and `ladder`. Execution briefs still
follow the attempt ladder below. The route table controls each tier's model and effort.

## Codex dispatch

Invoke this skill with `$dispatch`. Call `spawn_agent` with the installed
`agent_type`, a stable `task_name`, the four-field brief in `message`, and
`fork_turns="none"`. Omit `model` and `reasoning_effort`; the role TOML is the
authoritative pin. Model arguments still receive the shared exclusion and up-code
checks according to `router.modes`; their presence alone is not a denial.

Default roles map sweeps to gpt-6-luna/low, execution to gpt-6-sol/medium, and
judges or upper execution to gpt-6-astra/high. The installed role TOMLs are editable.
Standard sweeps use sol/medium; upper sweeps and other upper roles use astra/high.
Non-execution upper roles cannot provide a visible up code through the encrypted
message and are denied when `block_model` is enforced. Only execution upper roles
express the ladder request, and only with `ladder=enforce` after two accepted rounds.

The hook cannot read `message`: Codex encrypts it in the hook payload. Validate
TASK, FILES, BAR, and RETURN here before spawning. Each role's
`developer_instructions` must also reject a missing, empty, or duplicate field
before work. Risk words, four-field lint, and prompt `route:` directives are not
Codex hook checks. Send any extra findings as notes below the four fields.

Keep the exact same `task_name` and base role across retries. Use `builder`
for the first two accepted attempts, then explicitly choose `builder-up` for
round three. The same applies to the `builder-in-place` family. The hook rejects
upper execution before two accepted standard attempts, rejects standard execution
on round three, and blocks round four for owner replanning. Switching tier aliases
cannot reset history; changing sessions or spawning from another agent cannot
either. Never change task names or role families to evade the cap.

`test-writer` and `docs-writer` use the execution ladder but have no tier files.
On Claude, their third attempt uses model injection. On Codex, their pinned base
TOML cannot express the upper attempt; return to the owner after two attempts.

## Verify every return

The worker first runs its own BAR and returns evidence. The owner checks changed
paths and the return, then gives a new `judge` only the original brief, the
return, and the target worktree or change reference. Do this for every return,
including evidence sweeps. Do not resume an earlier judge or pass it the worker's
conversation. The author never grades its own work.

The judge stays read-only, opens the actual files, and re-runs the BAR itself.
It checks whether TASK is met, whether every changed path is inside FILES, and
whether the return is accurate. Green tests do not excuse missing requirements.
It never fixes findings. Its entire return is one verdict line: `PASS`, or
`SEND_BACK: <numbered findings>` with REPRODUCED or REASONED evidence.

On Codex, read-only behavior is requested by the role's instructions, not enforced.
As of Codex CLI 0.156, role files ignore `sandbox_mode`, `default_permissions`, and
`[permissions]`, so they cannot enforce read-only access. The child inherits the
parent session's sandbox. Read-only access is enforced only if the parent starts with
a restricted permission profile defined in the user/session configuration and
selected with `-c default_permissions=<profile>`. The `-s` flag overrides that
profile.

Integrate only after PASS. Preserve unrelated changes and follow the project's
normal review and integration rules. A judge's PASS is evidence of completion,
not permission for a deployment or other external action.

## Attempt ladder

On Claude:

1. Start an execution brief on Sonnet.
2. On failure or SEND_BACK, return the same brief to Sonnet once with the gap named.
3. If it still fails, use an Opus execution attempt with `route: up ladder` on its
   own prompt line. Preserve TASK and FILES and include the judge's findings.
4. If that attempt fails, return the unresolved brief to the owner. Stop retrying.

The hook tracks accepted execution spawns, so an interrupted attempt also uses a
round. For one TASK/FILES pair, the first two attempts use Sonnet, the third requires
`route: up ladder` and uses Opus, and a fourth is blocked. Judge calls do not consume
execution rounds. Never invent a new brief identity to evade the cap.

## Helpers and stopping

The installer places the helper scripts under
`${CLAUDE_HOME:-$HOME/.claude}/router/bin` for Claude, or
`${CODEX_HOME:-$HOME/.codex}/router/bin` for Codex:

- `close-lane.sh <worktree> <brief-file>` checks FILES and runs the BAR, printing
  PASS or FAIL. This mechanical check supports the judge; it does not replace it.
- `wait-until.sh` waits with a deadline. Exit 124 means timeout; report it and stop.
- `cite-check.py` checks supported file citations before evidence is accepted.

Use `router lanes` and `router report` to see the verdict ledger and attempt history.
Consult each helper's `--help` for arguments. Record actual results rather than predictions.

Keep returns short. Use targeted file reads, bounded commands, and explicit scope.
Do not include secrets in briefs, citations, run logs, or returns.

Run `router off` to disable all routing and context hooks. Run `router on` to
enable them again, and `router status` to inspect the switch. `ROUTER_OFF=1` is
also a process-level kill switch. Disabling routing does not cancel running seats.

Use `router auto off|suggest|nudge|enforce` to choose automatic routing (default `nudge`); `suggest` adds role hints and `enforce` requires a spawn after main-session read or edit thresholds.
