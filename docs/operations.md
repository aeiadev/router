# Operations briefs and independent observation

Use an operations brief when the target is published state, a service or another
non-Git resource. Existing `TASK` / `FILES` / `BAR` / `RETURN` repository briefs
remain valid. An operations brief never creates a worktree requirement or grants
permission. Keep the action within the user's existing authorization.

```text
PROFILE: operations/v1
OBJECTIVE: Correct the disposable fixture title.
TARGET: The explicitly selected fixture page.
ACTION: Update its title once.
SCOPE: Title only; preserve body and all other pages.
CONSTRAINTS: Use the named connector and its existing permissions.
PROOF: The resulting title equals the predeclared expected value.
READBACK: Read the selected page independently after the update.
RETURN: Report result, evidence reference and any missing proof.
REMOTE_IDENTITY: Verify the selected disposable endpoint before writing.
IDEMPOTENCY: Do not repeat a successful update.
ROLLBACK: Restore the recorded old title only if separately authorized.
```

The first line is exact. Required fields are unique, ordered and nonempty; the
last three are optional, in that order. The brief is at most 16 KiB, with at most
4096 characters per field. Do not mix repository fields into this profile.
Eligible roles are `worker`, `sweeper`, `researcher`, `planner` and `judge`.
Builders, test-writers and docs-writers retain their repository brief and
worktree contract. This profile is available in 0.4.0.

## Check capabilities before dispatch

`router operations check --file request.json` reads at most 64 KiB of JSON from a regular file or `-` for
stdin. Supply exactly `brief`, `agent`, `required_tools`, and `available_tools`.
Tool names must be exact. Set `available_tools` to `null` when visibility is
unknown; an empty list means none are declared. A wildcard does not establish
read-only authority or tool visibility.

From a 0.4.1 or later checkout, run this complete synthetic preflight. It
requests the custom agent `fixture-worker` unchanged and deliberately lacks a
declared tool, so the command exits 3 without dispatching anything.

```bash
python3 -B bin/router operations check --file - <<'JSON'
{"brief":"PROFILE: operations/v1\nOBJECTIVE: Correct the disposable fixture title.\nTARGET: Fixture page page_1.\nACTION: Set the title to Fixture title.\nSCOPE: Title only.\nCONSTRAINTS: Use the named connector with existing authorization.\nPROOF: Title digest matches the predeclared digest.\nREADBACK: Read page_1 through a separate read-only observer.\nRETURN: Report result and evidence reference.","agent":"fixture-worker","required_tools":["fixture.read","fixture.write"],"available_tools":["fixture.read"]}
JSON
```

Set `available_tools` to `null` to get `tool_status: unknown` and exit 0;
that status still requires a host visibility check. For a script-installed
Harness checkout, use `~/.claude/harness/hooks/checkpoint/operations_cli.py`
or `~/.codex/harness/hooks/checkpoint/operations_cli.py` in place of the
checkout path. Router installs its `router` command under the corresponding
host home. These commands ship with 0.4.0.

The coordinator must run the check **before** calling `Agent` or `spawn_agent`:

1. Validate the selected target, action and existing authorization.
2. Build the check from the original brief and the user's named agent's declared
   tools. Exit 3 means a declared tool is missing: return the diagnosis without
   spending a worker call. Other nonzero exits mean invalid input.
3. Read the report even after exit 0. `unknown` remains unresolved; verify actual
   host visibility before a consequential action. `declared_available` is only
   a declaration, never live verification.
4. If dispatch is justified, pass the original agent name, model, tools,
   permissions and payload unchanged. This helper never dispatches or replaces
   a custom agent. Native/custom passthrough and Codex opaque-prompt limits remain.

## Observe resulting state

Prewrite an expected proof using schema version 1, opaque task/target/revision
IDs, and a nonempty `requirements` mapping requirement IDs to SHA-256 digests.
Use a separate observer restricted by connector credentials, server authority or
host permissions. Role prose alone is insufficient. Read actual resulting state;
do not replay the update to infer its success.

`router observe --file proof.json` reads at most 64 KiB of JSON with exactly `expected` and `observed`.
For a complete synthetic matching proof, run this report-only command
from the checkout:

```bash
python3 -B bin/router observe --file - <<'JSON'
{"expected":{"schema_version":1,"task_id":"task_1","target_id":"page_1","revision_id":"rev_2","requirements":{"title":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},"observed":{"schema_version":1,"task_id":"task_1","target_id":"page_1","revision_id":"rev_2","requirements":{"title":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"read_status":"ok","evidence_id":"evidence_1","verifier_id":"observer_1"}}
JSON
```

This example reports `PASS`. Replace `observed` with
`{"schema_version":1,"read_status":"unavailable"}` to report `UNKNOWN`.
For a complete read, `observed` has `read_status: "ok"`, the same IDs, the observed requirement digests, and opaque
`evidence_id` and `verifier_id`. Missing evidence or denied/unavailable reads
produce `UNKNOWN`; complete wrong target/revision/content produces `FAIL`;
complete matching proof produces `PASS`. Invalid schemas are errors. Digests and
IDs must be derived from real readback by the consumer; the command cannot verify
the truth of supplied JSON.

All three valid verdicts exit 0: this is a report, not a publishing gate. The
report never repairs, retries, mutates or automatically accepts work. It always
states `host_verified: false`, `independent_acceptance: "unverified"`, and
`gates_mutation: false`. An explicit consumer may implement a separate gate.
Record evidence references and retain detailed artifacts privately; do not put
raw page contents, credentials, URLs or private paths into receipts.

The shipped tests use an isolated fake service with separate worker and observer
channels. They prove fixture write denial, not live SSH/MCP safety or host trust.
The tools do not inspect arbitrary remote shell commands or connector mutations.

## Paired workflow evidence

`tests/test_operations_ssh.py` runs the workflow against the Harness disposable
OpenSSH fixture: preflight, the Harness guard, an upload and rename that reach the
container, a prohibited removal denied before dispatch, and observer readback of the
actual bytes. Run it with `HARNESS_DIR=/absolute/harness python3 -B tests/test_operations_ssh.py`.
It skips with an "unrun" message unless `SSH_FIXTURE_REQUIRED=1` (or
`SSH_FIXTURE_RUN=1`) is set; with `SSH_FIXTURE_REQUIRED=1` a missing checkout,
engine or image fails. It covers synthetic content on loopback only, not an
installed host, a live connector or a model. See `docs/host-validation.md` for the
three evidence classes and the on-demand host checklist. The brief stays
`operations/v1`.
