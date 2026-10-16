# Paired operations SSH fixture

`tests/test_operations_ssh.py` has no fixture files of its own. It loads the
disposable OpenSSH fixture and the command guard from the Harness checkout named
by `HARNESS_DIR` (`tests/fixtures/ssh-disposable/run.py` and
`hooks/pyrun.sh harness block guard/danger_cmd_guard.py`). There is no second
SSH parser, policy reader or server here; change those in Harness only.

```sh
HARNESS_DIR=/absolute/path/to/harness python3 -B tests/test_operations_ssh.py
HARNESS_DIR=/absolute/path/to/harness SSH_FIXTURE_REQUIRED=1 python3 -B tests/test_operations_ssh.py
```

- Without `SSH_FIXTURE_RUN=1` or `SSH_FIXTURE_REQUIRED=1` the real tests skip and
  say the proof is unrun.
- With `SSH_FIXTURE_REQUIRED=1`, a missing `HARNESS_DIR`, container engine, built
  fixture image or guard fails the run. It is never reported as a skip.
- The Harness image must already be built (`run.py --build` in that checkout).
  The container has loopback only, no host mounts and fresh fixture-only keys.

What the real tests exercise: `router operations check` stops on a declared
missing tool and leaves unknown visibility unknown; the Harness guard allows an
authorized upload and rename that then reach the container, and denies a
prohibited removal before any remote process starts; the separate observer
credential reads the resulting bytes, and `router observe` reports PASS, FAIL
(stale or mismatched) or UNKNOWN (denied or unavailable) without mutating the
server. Supplied JSON is shown to be unauthenticated.

Rootless Podman keeps its image storage under `HOME`. Do not run the real tests with a
disposable or changed `HOME`: the built fixture image is then not found and the required run
fails loudly with "pinned fixture image is not built" (it is never a skip). The tests give
their own subprocesses a disposable `HOME`; the engine call keeps yours.

This is a disposable-server test with synthetic content. It does not exercise an
installed Claude Code or Codex hook, a live connector, a model or a paid provider.
