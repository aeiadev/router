<!-- router:defaults:start (managed by router install --with-defaults; edits here are replaced) -->
## Delegation defaults

- Do it here when it is one question, one file, one answer. Delegating that
  costs more than doing it.
- The main session plans, writes briefs and checks results. Hand work to a
  role when it touches more than {{files_threshold}} files or means reading a log or transcript.
  As a guide, more than {{command_threshold}} commands of digging also deserves a role; the router does not count commands.
  {{reminder}}
- Route by kind of work: find, list, count -> sweeper. Evidence for a
  decision -> researcher. Critique a plan -> planner. Change code -> builder
  (own worktree) or builder-in-place (this directory). Several steps -> worker.
  Failing tests first -> test-writer. Docs -> docs-writer.
- Use $dispatch and spawn_agent with a stable task_name for each brief.
- Every brief has TASK, FILES, BAR, RETURN. Send independent briefs together.
- Check every build: run the BAR yourself first; if it passes, send the work
  to a fresh judge. The author never judges its own work.
- A failed brief goes back once to builder with the judge's findings, then once to builder-up with the same task_name, then back to you to replan.
- Returns stay under {{return_limits}}; anything longer goes to a file and the return names it.
<!-- router:defaults:end -->
