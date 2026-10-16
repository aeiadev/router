#!/bin/sh
# pyrun.sh: run a Router or Harness Python entry point with a real Python 3.9 or newer.
#   sh pyrun.sh <router|harness> <warn|block|status> <script> [args]
#       script: a path beside this file, an absolute path, or - for a script on stdin
#   sh pyrun.sh --find <router|harness>    print the interpreter that would run; writes nothing
# Lookup: $ROUTER_PYTHON or $HARNESS_PYTHON, then the cached <state>/claude-<tool>/python (state is
# $XDG_STATE_HOME when absolute, else $HOME/.local/state), then python3, python and py -3.
# Router expands a leading ~ in $XDG_STATE_HOME before checking whether it is absolute.
# Router's nonempty $ROUTER_STATE overrides its state directory, including a leading ~ or ~user.
# A candidate counts only when it prints its own path for version 3.9+; the Store stub prints nothing.
# No Python: warn prints one stderr line and exits 1, block exits 2, status prints on stdout, exits 0.
# POSIX sh (dash, bash 3.2, Git Bash). Router and Harness ship this file byte for byte.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
if [ "${1-}" = --find ]; then find=1; tool=${2-}; mode=warn; else find=; tool=${1-}; mode=${2-}; fi
case $tool in
  router) name=ROUTER_PYTHON; py=${ROUTER_PYTHON-} ;;
  harness) name=HARNESS_PYTHON; py=${HARNESS_PYTHON-} ;;
  *) echo "pyrun: unknown tool '$tool' (router or harness)" >&2; exit 1 ;;
esac
case $mode in
  warn|block|status) ;;
  *) echo "pyrun: unknown mode '$mode' (warn, block or status)" >&2; exit 1 ;;
esac
usable() { [ -n "$1" ] && command -v "$1" >/dev/null 2>&1; }
probed=; discovered=
probe_python() {
  [ -z "$probed" ] || { [ -n "$discovered" ]; return; }
  probed=1
  for candidate in python3 python 'py -3'; do
    command -v "${candidate%% *}" >/dev/null 2>&1 || continue
    # shellcheck disable=SC2086 # py -3 is two words
    found=$($candidate -c 'import os, sys
if sys.version_info >= (3, 9): sys.stdout.write(sys.executable.replace(os.sep, "/"))' 2>/dev/null) || continue
    case $found in */WindowsApps/*) found=$(command -v "${candidate%% *}") ;; esac
    if usable "$found"; then discovered=$found; return 0; fi
  done
  return 1
}
cache_ok=1
# Git Bash identifies its native platform even when MSYSTEM is not inherited.
windows=
case ${OSTYPE-} in msys*|cygwin*) windows=1 ;; esac
state_home=${HOME-}
if [ -z "$windows" ]; then
  # Literal shell tilde expansion uses the account database when HOME is unset.
  # Empty HOME is distinct and is resolved below using the selected interpreter.
  if [ "${HOME+x}" != x ]; then
    state_home=~
    if [ "$state_home" = '~' ]; then
      # dash leaves the tilde literal. getent follows the same account database
      # as Python's pwd.getpwuid; fixed system paths also work with an empty PATH.
      account_id=$(/usr/bin/id -u 2>/dev/null || /bin/id -u 2>/dev/null)
      account=$(/usr/bin/getent passwd "$account_id" 2>/dev/null || /bin/getent passwd "$account_id" 2>/dev/null)
      if [ -n "$account" ]; then
        IFS=: read -r account_name account_pass account_uid account_gid account_gecos state_home account_shell <<EOF
$account
EOF
      else cache_ok=; fi
    fi
  elif [ -z "$state_home" ]; then state_home=/; fi
fi
if [ -n "$windows" ] && { [ "$tool" = harness ] || [ -z "$state_home" ]; }; then
  state_home=${USERPROFILE-${HOMEDRIVE-}${HOMEPATH-}}
  # Native Path.home() represents an explicitly empty USERPROFILE as '.'.
  [ -n "$state_home" ] || state_home=.
fi
state_converted() {
  # Git Bash normally converts slash-rooted environment paths for native Python.
  # Its exclusion list contains variable-name prefixes, separated by semicolons.
  exclusions=${MSYS2_ENV_CONV_EXCL-}
  while [ -n "$exclusions" ]; do
    prefix=${exclusions%%;*}
    case $prefix in '*') return 1 ;; '') ;; *) case $1= in "$prefix"*) return 1 ;; esac ;; esac
    case $exclusions in *';'*) exclusions=${exclusions#*;} ;; *) break ;; esac
  done
  return 0
}
absolute_state() {
  if [ -z "$windows" ]; then case $state in /*) return 0 ;; esac
  else
    case $state in
      [A-Za-z]:[/\\]*) return 0 ;;
      [/\\][/\\]*)
        # UNC needs both a server and a nonempty share, not just two slashes.
        unc=${state#??}; server=${unc%%[/\\]*}
        case $unc in
          *[/\\]*) share=${unc#*[/\\]}
                    share=${share%%[/\\]*}
                    [ -n "$server" ] && [ -n "$share" ]; return ;;
        esac
        return 1 ;;
      /*) if [ -n "$expanded_home" ]; then state_converted HOME && return 0
          else state_converted XDG_STATE_HOME && return 0; fi ;;
    esac
  fi
  return 1
}
expand_state() {
  expanded_home=; resolved=
  case $state in
    '~') state=$state_home; expanded_home=1; return ;;
    '~/'*|'~\'*)
      # compat.expanduser joins HOME and the suffix as pathlib does: a rooted suffix
      # replaces HOME's path (POSIX) or keeps only HOME's drive (Windows).
      suffix=${state#??}
      case $windows:$suffix in
        :/*) state=$suffix; return ;;
        1:[/\\]|1:[/\\][!/\\]*)
          case $state_home in
            [A-Za-z]:*) state=${state_home%"${state_home#??}"}$suffix; return ;;
            /[A-Za-z]/*) home_drive=${state_home#/}; state=${home_drive%%/*}:$suffix; return ;;
          esac ;;
        1:[/\\]*|1:?:*) ;;
        *) state=${state_home%/}/$suffix; expanded_home=1; return ;;
      esac ;;
    '~'*) ;;
    *) return ;;
  esac
  # Named users and the remaining native rooted or drive suffixes (parsed differently
  # across Python versions) take the whole cache path from the interpreter's pathlib,
  # built as compat.expanduser and common.state_path build it, without evaluating path
  # text. Ordinary warm paths stay shell-only.
  resolver=$py
  if ! usable "$resolver"; then probe_python || :; resolver=$discovered; fi
  if [ -n "$resolver" ] && state=$("$resolver" -c 'import os, sys
from pathlib import Path
def home():
    value = os.environ.get("HOME", "")
    if not value:
        return Path.home()
    if os.name == "nt" and len(value) >= 3 and value[0] == "/" and value[1].isalpha() and value[2] == "/":
        value = value[1].upper() + ":" + value[2:]
    return Path(value)
text = sys.argv[1]
path = home() / text[2:] if text[:2] in ("~/", "~\\") else Path(text).expanduser()
if sys.argv[2] == "xdg":
    text = str(path).replace("\\", "/")
    parts = text[2:].split("/")
    absolute = path.is_absolute() and (os.name != "nt" or not text.startswith("//")
                                      or len(parts) >= 2 and all(parts[:2]))
    path = (path if absolute else home() / ".local/state") / "claude-router"
sys.stdout.write(str(path / "python"))' "$state" "$1" 2>/dev/null); then resolved=1
  else
    # An unknown user also fails in compat.expanduser. Do not read or write a
    # different cache; let the actual entry point retain its own error behavior.
    cache_ok=
  fi
}
select_cache() {
resolved=
if [ "$tool" = router ] && [ -n "${ROUTER_STATE-}" ]; then
  state=$ROUTER_STATE
  expand_state router
  if [ -n "$resolved" ]; then cache=$state; return; fi
  if [ -n "$windows" ] && [ -z "$expanded_home" ] && ! state_converted ROUTER_STATE; then
    # A native root-relative path stays on the child's current drive. Backslash
    # spelling prevents Git Bash from remapping it to its own / or /c mount.
    case $state in /|/[!/]*) state=\\${state#/} ;; esac
  fi
  case $windows:$state in
    1:[A-Za-z]:|1:\\) cache=${state}python ;;
    *) cache=${state%/}/python ;;
  esac
else
  state=${XDG_STATE_HOME-}
  if [ "$tool" = router ]; then expand_state xdg; fi
  if [ -n "$resolved" ]; then cache=$state; return; fi
  absolute_state || state=${state_home%/}/.local/state
  cache=${state%/}/claude-$tool/python
fi
}
read_cached_python() {
  cached=
  if [ -n "$cache_ok" ] && [ -f "$cache" ]; then IFS= read -r cached < "$cache" || :; fi
}
if [ -z "$windows" ] && [ "${HOME+x}" = x ] && [ -z "$HOME" ]; then
  # Path.home() for empty HOME is '.' on 3.9 but '/' on newer Python.
  # This exceptional branch can still bootstrap with only a cached interpreter.
  resolver=$py
  if ! usable "$resolver"; then
    for state_home in / .; do
      select_cache
      candidate_cache=$cache
      read_cached_python
      usable "$cached" || continue
      candidate_python=$cached
      state_home=$("$candidate_python" -c 'from pathlib import Path; print(Path.home())' 2>/dev/null) || continue
      select_cache
      # A cached interpreter must agree that its own cache belongs at this path.
      if [ "$cache" = "$candidate_cache" ]; then py=$candidate_python; resolver=$py; break; fi
    done
  fi
  if ! usable "$resolver"; then probe_python || :; resolver=$discovered; fi
  if usable "$resolver"; then
    state_home=$("$resolver" -c 'from pathlib import Path; print(Path.home())' 2>/dev/null) || cache_ok=
  else cache_ok=; fi
fi
select_cache
if ! usable "$py"; then
  py=
  read_cached_python
  py=$cached
fi
commit=
if ! usable "$py"; then
  probe_python || :
  py=$discovered
  # Cache only in the tool's existing state folder (its hooks make it): read-only commands write nothing.
  # Python saves it below, and only once the script exits 0: a rejected run leaves no cache.
  cache_dir=${cache%[/\\]*}  # an interpreter-built native cache path uses backslashes
  if [ -n "$py" ] && [ -n "$cache_ok" ] && [ -z "$find" ] && [ -d "$cache_dir" ]; then commit=1; fi
fi
if [ -z "$py" ]; then
  message="$tool: no Python 3.9 or newer found (python3, python, py -3); install one or set $name"
  case $mode in
    status) printf '%s\n' "$message"; exit 0 ;;
    block) printf '%s\n' "$message" >&2; exit 2 ;;
    *) printf '%s\n' "$message" >&2; exit 1 ;;
  esac
fi
if [ -n "$find" ]; then printf '%s\n' "$py"; exit 0; fi
script=${3-}
if [ -z "$script" ]; then echo "pyrun: missing script" >&2; exit 1; fi
shift 3
case $script in
  -|/*|[A-Za-z]:[/\\]*) ;;
  *) here=${0%/*}; [ "$here" != "$0" ] || here=${0%\\*}; [ "$here" != "$0" ] || here=.  # C:\x\pyrun.sh too
     script=$here/$script ;;
esac
if [ -n "${MSYSTEM-}" ]; then
  # Git Bash spells C:\x as /c/x; Windows Python needs c:/x.
  case $script in /[A-Za-z]/*) drive=${script#/}; drive=${drive%%/*}; script=$drive:${script#/?} ;; esac
fi
case $script in *.pyc|*.pyo|*.pyz|*.zip) commit= ;; esac
if [ -n "$commit" ] && [ "$script" != - ] && [ -f "$script" ] && [ -r "$script" ]; then
  # Only a cold run of a readable script file runs through this boot. A compiled or zipped one
  # (*.pyc, *.pyo, *.pyz, *.zip), a folder, stdin and every other run are the plain exec at the
  # end and save nothing. The boot reads the script as source, so a compiled or zipped script
  # under another name fails here where a plain run would not. For a source script it keeps what
  # `python script` gives the script: sys.argv, sys.path[0] (the folder of the script's real
  # path; on Windows CPython does not resolve symlinks there), __name__, __file__ and __loader__
  # (on Windows: a C:\ or \\server script path as given on Python 3.11+, else its full path)
  # in a fresh __main__ module, the exit status, and the message or traceback an uncaught
  # exception prints, except that on Python 3.9 and 3.10 a KeyboardInterrupt traceback also
  # lists the boot frames. It does not keep sys.orig_argv, the call stack (the script runs one
  # boot frame deeper), sys.modules (the boot imports importlib.machinery) or, when an uncaught
  # exception ends the run, the order of stdout and stderr on one shared pipe (the traceback
  # comes before buffered stdout). The cache is saved
  # only after a normal finish or a SystemExit with code None or 0 and a successful flush of
  # stdout and stderr, before atexit handlers run: a private temporary file, then a rename,
  # never into a folder it would have to create. Python 3.9 syntax, no single quotes.
  boot='fresh = dict(globals())
import io, os, sys
cache, found, script = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.argv = sys.argv[3:]
if os.name == "nt":
    path = script if sys.version_info >= (3, 11) and (script[1:3] == ":\\" or script[:2] == "\\\\") else os.path.abspath(script)
elif os.path.isabs(script):
    path = script
else:
    path = os.path.join(os.getcwd(), script)
if not getattr(sys.flags, "safe_path", False):
    sys.path[0] = os.path.dirname(os.path.realpath(script))
saved = False
try:
    with io.open_code(path) as handle:
        source = handle.read()
    from importlib.machinery import SourceFileLoader
    main = type(sys)("__main__")
    vars(main).update(fresh, __file__=path, __cached__=None, __loader__=SourceFileLoader("__main__", path))
    sys.modules["__main__"] = main
    exec(compile(source, path, "exec", dont_inherit=True), vars(main))
    saved = True
except SystemExit as stop:
    saved = stop.code is None or (isinstance(stop.code, int) and stop.code == 0)
    raise
except BaseException as error:
    trace = error.__traceback__
    while trace is not None and trace.tb_frame.f_globals is globals():
        trace = trace.tb_next
    error.with_traceback(trace)
    if isinstance(error, KeyboardInterrupt):
        raise
    sys.excepthook(type(error), error, trace)
    sys.exit(1)
finally:
    for output in (sys.stdout, sys.stderr):
        if saved and output is not None and not getattr(output, "closed", False):
            try:
                output.flush()
            except Exception:
                saved = False
    if saved:
        temporary = "%s.%d" % (cache, os.getpid())
        try:
            data = os.fsencode(found) + b"\n"
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except (OSError, ValueError):
            pass
        else:
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, cache)
            except OSError:
                try:
                    os.remove(temporary)
                except OSError:
                    pass'
  exec "$py" -c "$boot" "$cache" "$py" "$script" "$@"
fi
exec "$py" "$script" "$@"
