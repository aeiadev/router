# Host validation for the paired operations workflow

This page is an on-demand checklist. Router keeps no qualification registry and
sends no telemetry; `router doctor` runs no live probe. Record results in your own
notes, outside Router state. Anything not run stays **unrun**, never "passed".

## Evidence classes

Keep these separate. A result in one class never stands in for another.

1. **Offline adapter tests.** `tests/test_operations.py`, `tests/test_observation.py`
   and `tests/test_phase_cd.py` call Router's own commands with synthetic input.
   They show the preflight and report contracts, not any host.
2. **Installed executable and hook tests.** `tests/together.sh` installs Router
   and Harness into a disposable home and runs the installed hook scripts.
   `tests/test_operations_ssh.py` with `HARNESS_DIR` and `SSH_FIXTURE_REQUIRED=1`
   runs the accepted Harness disposable OpenSSH container: the Harness guard, a
   worker credential and a separate observer credential on synthetic bytes. It
   needs a container engine and a built Harness fixture image, and fails when
   they are missing. It does not start Claude Code or Codex.
3. **Live host, connector or provider tests.** A real installed Claude Code or
   Codex session, a real connector and a real model. None of these have been run
   for this release. Static or fixture tests cannot satisfy this class.

## Unrun here

Unrun in this repository's tests: native Claude Code and Codex agent spawning,
hook trust prompts, custom-agent model preservation inside a host, connector
visibility, macOS and Windows SSH behavior, and any paid-model pilot.

## On-demand checklist

Do these by hand, on the machine you intend to rely on, only with a disposable
target and benign connector. Write the observed answer next to each line.

- [ ] Installed Claude Code version (`claude --version`) and installed Codex version (`codex --version`), copied exactly.
- [ ] Router and Harness versions from `install.sh --status`, and the checkout each came from.
- [ ] Hook trust: Claude settings list the hooks; Codex shows the hooks reviewed and trusted after the last change to a hook script.
- [ ] Hook invocation: one harmless action visibly passes through the installed hook (a refused `rm` of a scratch file in a disposable project shows the guard's message).
- [ ] Custom agent identity: spawn your own named agent once with a benign brief. Its name, model, tools and permission mode in the host's own record equal what you declared. Router must not have replaced it.
- [ ] Native roles: the same check for any native role you intend to use, with the model the host reports.
- [ ] Benign connector availability: the host lists the connector's read tool, and a read of a disposable record succeeds. `available_tools: null` stays unknown until you do this.
- [ ] Observer authority, shown separately: the observer credential reads the resulting state and is refused a write by the server or connector, not by role prose.
- [ ] Readback: compare the digest of the bytes the observer read with the predeclared digest using `router observe`. A hand-written proof is not readback.

A live model or provider pilot needs an agreed budget stated before it starts. If
none was agreed, that item stays unrun.

## What a pass means

`router operations check` and `router observe` stay report-only, `operations/v1`
unchanged. Even a full checklist records what you saw on one machine at one time.
It does not qualify other hosts, versions or servers.
