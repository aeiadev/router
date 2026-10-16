# Commands-only installation lifecycle

Router's public adapter is an explicit opt-in: `bash install.sh --profile commands
--host claude --dry-run > plan.json`, review the JSON, then `bash install.sh
--profile commands --host claude --plan plan.json`. Use `codex` instead of
`claude` for the other host. `--host both` is refused for this profile. The
dry run leaves HOME, host settings, hooks, roles, instructions and command
state unchanged. It removes its temporary payload before returning; apply
recreates the exact saved inventory at that path. Keep the saved plan and use
the same `TMPDIR` for apply. The recreated payload remains available for
interruption recovery. A changed payload or preimage requires a new plan.
`--status`, `--rollback`, `--recover` and `--uninstall`
are profile-scoped and require the same explicit host. Uninstall preserves
command data and custom host settings. An existing private `claude-router`
launcher is a conflict and is never adopted.

Installed launchers are `~/.local/bin/claude-router` and
`~/.local/bin/codex-router`. They run from the private
`~/.claude/router-commands` and `~/.codex/router-commands` runtimes without
the source checkout. Only `operations`, `observe`, `receipt`, `usage`,
`evaluate`, `status`, `doctor`, `help` and `version` are available. Routing
switches and configuration commands are rejected. Default command state is
`$XDG_STATE_HOME/router-commands/<host>` or
`~/.local/state/router-commands/<host>`; `ROUTER_COMMANDS_STATE` overrides it
for one invocation. Legacy `ROUTER_STATE` and routing variables do not select
this store. Status reports commands-only mode, version, source revision,
manifest digest, unregistered hooks and unknown host availability. The
profile does not activate interception or establish MCP qualification.

The shared installer helper is `scripts/commands_install.py`. It manages one
explicit product (`router` or `harness`) and one explicit host (`claude` or
`codex`) per call. `--host both` is rejected. It does not alter host hooks,
settings, routing defaults, or command data. The existing full installer has
its own lifecycle.

Router and Harness commands profiles can be installed in either order and for
either host in one home. Each uses its own runtime (`<host-home>/<product>-commands`),
launcher (`~/.local/bin/<host>-<product>`) and command data
(`$XDG_STATE_HOME/<product>-commands/<host>`). The shared helper is the same bytes in
both checkouts. The full installers and their uninstall leave both profiles unchanged.
A private launcher with the same name conflicts with only its own product and is
never adopted. Run `HARNESS_DIR=/absolute/harness bash tests/together.sh` to verify
the pair in disposable homes; a run without `HARNESS_DIR` skips and is incomplete.

On POSIX, the CLI refuses a umask that removes owner permissions before
creating locks or installation state. Common masks `000`, `022` and `077`
are supported; explicitly requested private creation modes remain private.
The CLI preserves the invoking process's mask.

The installer checks the complete lifecycle path envelope before publishing
bootstrap state, including transaction, staging, backup, recovery and temporary
filenames. POSIX limits come from the destination filesystem and count encoded
bytes. A staged file needs 41 additional bytes in its final filename component.
Existing pending and committed journals are checked before recovery or reversal.
Cleanup of an older failed bootstrap checks only its actual partial material,
so an unused overlong future journal path cannot prevent removing its own empty
directories. Unknown material remains a refusal. Exhausting the 16-digit
transaction sequence refuses a new apply before publishing bootstrap state.
Native Windows queries the volume component limit and counts UTF-16 units; this
profile currently refuses layouts beyond ordinary Win32 file/directory budgets
with `Windows long-path lifecycle is unqualified`. A registry setting alone does
not establish the running process's long-path capability. No setting is changed
and no probe files are created during a dry run. See Microsoft's
[path limits](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation).

On POSIX, prepare a private payload directory (mode `0700`) containing only the
runtime files to install and `manifest.json` (mode `0600`). On Windows, these
manifest modes describe intended file roles; observed read-only flags are checked
instead of POSIX access bits. Windows payload files must declare owner-write
permission so rollback and uninstall can remove them; read-only payloads are
refused before installation. The helper does not change or qualify Windows ACLs.
The manifest has schema 1,
a nonempty `source_revision`, and a `files` map of strict relative paths to
`sha256` and integer `mode`. The map must describe every payload file except
the manifest. Include executable `bin/router-commands` or
`bin/harness-commands` as appropriate. Extra files, links, devices, unsafe
paths, or changed bytes refuse installation. Keep all payload directories
private using the operating system's access controls. Every directory and file
must satisfy the same path, type and
permission rules used by recovery journals; POSIX special permission bits and unsafe
empty directory names are refused before mutation. The helper verifies the
payload again during apply.

Router's adapter sets `source_revision` to `sha256:` plus the digest of the
packaged file inventory and VERSION. This identifies the runtime bytes even
from a release archive without `.git`. The installed entry checks every
packaged file against its manifest before loading command modules. Its shell
launcher selects Python 3.9+ from `python3`, `python`, or `py -3`.

Saved plans and lifecycle JSON records have a 1 MiB limit. Planning and apply
reserve space for staging, interruption recovery and rollback before changing
the installation. Recovery also checks that older records can accommodate their
remaining updates; an oversized record is refused without starting a mutation.

For example, with a prepared Router payload, run `python3 -B
scripts/commands_install.py plan --product router --host claude --home
"$HOME" --payload "$PAYLOAD" > plan.json`, review the JSON, then run the
same arguments with `apply --plan plan.json` replacing `plan`. Planning is
write-free. The saved plan binds the product, host, home, host home, payload
path, source revision, exact payload inventory, parent modes, ownership markers,
and current installation preimage. The generated `identity` object uses the same
canonical home, host home, runtime, launcher, installer state, product and host in
plans and journals. Regenerate plans made by earlier helper revisions. Any change requires a new plan. The caller may pass
`--host-home` to honor an existing host-home override. `--payload` is required
for plan and apply.

`status`, `rollback`, `recover`, and `uninstall` take the same product, host,
and home arguments. Rollback restores the latest committed preimage. Uninstall
rolls back unchanged owned upgrades until the initial absent preimage. A
pending transaction requires `recover` before another operation. Recovery
restores the preimage after an interrupted apply and resumes an interrupted
rollback. If an owned file, stage, backup, launcher, or journal has unknown
drift, recovery refuses and leaves the material for manual inspection; remove
only the identified user addition or restore exact owned bytes before retrying.
Do not delete a transaction journal to force past a refusal.
Recovery checks the expected creation mode (`0700` on POSIX; writable directory
flags on Windows) on runtime, backup, stage and transaction roots, plus the
observed mode of every inventoried file and directory. It binds the journal to the
original home, host home, installer state, product, host, runtime, and launcher. A different `--home` with the same `XDG_STATE_HOME`
cannot recover that transaction. An interrupted stage file is recognized only
while its journal-bound temporary file matches a prefix of the original payload;
keep that payload unchanged and available until recovery finishes. Edited or
unverifiable partial files are left in place for manual repair.

Runtime paths are `<host-home>/<product>-commands`; launcher paths are
`~/.local/bin/<host>-<product>`. Journals live under
`$XDG_STATE_HOME/<product>-commands-installer/<host>` (default
`~/.local/state`). Runtime command data under
`$XDG_STATE_HOME/<product>-commands/<host>` is not installer-owned. The
existing private `claude-router` launcher conflicts with a Router/Claude
commands installation. It is never adopted automatically.

Before creating any parent directory, the helper publishes a bootstrap
journal (mode `0600` on POSIX) at `<home>/.<product>-commands-<host>-bootstrap.json`. Interrupted
bootstrap recovery validates the complete cleanup set, then removes only its
unchanged empty directories, journals and markers. Once the transaction journal
and markers are ready, bootstrap intent is removed and runtime staging begins.
Completed recovery retains transaction history; a fresh plan may be required.
Abandoned bootstrap temporary files are preserved; they do not authorize
any installation mutation. Unknown or edited temporary journals are never used
to authorize recovery. Do not remove ownership or bootstrap records to bypass a
refusal.

The helper locks each canonical runtime, launcher, installer state and bootstrap
path in a stable order. Homes sharing a runtime or launcher therefore serialize.
Persistent `.<runtime-name>.owner` and `.<launcher-name>.owner` symlinks beside
the destinations contain an identity digest (not a usable filesystem target).
They reserve destinations across process interruption, including when the runtime
is temporarily absent. Another home cannot adopt them. Recovery and uninstall
remove only matching markers when the installation returns to an absent preimage.
The CLI actions and payload-manifest interface are unchanged; consumers should
treat generated plan/journal identity and ownership fields as opaque.

POSIX uses fixed lock paths independent of `TMPDIR`: `/private/tmp` on macOS
and `/tmp` elsewhere, with no-follow opens
where supported, exclusive temporary creation, and explicit mode checks. Windows
normalization preserves drives and UNC roots and folds case; its locks use the
user's `LOCALAPPDATA` directory, independent of `--home` and `TMPDIR`. Unsupported
no-follow flags are omitted, and symlink creation requires OS permission. Windows
ACLs, reparse-point races and filesystem semantics are not equivalent to POSIX
mode/no-follow checks. Windows records the observed read-only flags in plans and
journals and refuses flag drift in owned material. Manifest executable bits stay
required as intent, since Windows `stat` does not report that intent. Absolute
launcher links tolerate only the ordinary Win32 substitution prefix; ownership
and exact expected destinations remain checked. No access-control setting is
weakened to make installation work. Complete native Windows lifecycle
qualification remains pending; the helper makes no ACL confidentiality or
POSIX-equivalent no-follow guarantee.

Installer state, default command data, runtime, launcher, owner markers and temporary/bootstrap paths
must be disjoint: none may equal or contain another reserved path. Bootstrap
temporary filename families are also reserved, including descendants of those
paths. Sharing an
ordinary ancestor is supported. The helper rejects overlapping layouts before
creating directories. A missing runtime is an absent installation only when its
active committed history also resolves to absence; removing runtime/launcher
files by hand cannot start a second installation history or release ownership.
Use the helper's uninstall, rollback or recovery actions. Completed historical
records do not prevent a later clean installation.
