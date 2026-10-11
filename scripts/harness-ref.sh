#!/usr/bin/env bash
# Picks the commit of the sibling repository that CI tests this one against. Router ships this
# file as scripts/harness-ref.sh and Harness as scripts/router-ref.sh; the bytes are the same.
#
# usage: <script> latest SIBLING_DIR VERSION_FILE
#   Prints the exact release tag v<VERSION> of SIBLING_DIR matching VERSION_FILE.
#   Prints nothing when that tag is absent; together requires the same package version.
#
# usage: <script> resolve SIBLING_DIR VERSION_FILE [PINNED_REF]
#   Prints a full commit id and names it on stderr. PINNED_REF (a branch, tag or commit of the
#   sibling), when set, wins. Otherwise: the sibling tag v<VERSION> if it exists; else the sibling
#   main when its VERSION is the same or newer; else fetch again every SIBLING_REF_INTERVAL
#   seconds (default 20), up to SIBLING_REF_TRIES times (default 30, so 10 minutes), because the
#   other repository may still be pushing its release; then fail with one line.
#
# SIBLING_DIR is a clone whose remote is origin. Bash 3.2 or newer; needs only git.
set -eu
name=${0##*/}
name=${name%.sh}

usage() {
  echo "usage: $name latest SIBLING_DIR VERSION_FILE" >&2
  echo "       $name resolve SIBLING_DIR VERSION_FILE [PINNED_REF]" >&2
  exit 2
}

is_version() {
  printf '%s\n' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'
}

# version_ge A B: true when version A is the same as or newer than version B.
version_ge() {
  local a b x y
  a=$1.
  b=$2.
  while [ -n "$a" ]; do
    x=${a%%.*}
    y=${b%%.*}
    a=${a#*.}
    b=${b#*.}
    if [ "$x" -gt "$y" ]; then return 0; fi
    if [ "$x" -lt "$y" ]; then return 1; fi
  done
  return 0
}

[ "$#" -ge 3 ] || usage
mode=$1
dir=$2
file=$3
case $mode in
  latest) [ "$#" -eq 3 ] || usage ;;
  resolve) [ "$#" -le 4 ] || usage ;;
  *) usage ;;
esac
pinned=${4:-}
label=${dir%/}
label=${label##*/}

version=$(head -n 1 "$file" 2>/dev/null | tr -d '\r') || version=
if ! is_version "$version"; then
  echo "$name: cannot read a version from $file" >&2
  exit 1
fi

if [ "$mode" = latest ]; then
  git -C "$dir" tag --list "v$version" || exit 1
  exit 0
fi

commit_of() {
  git -C "$dir" rev-parse -q --verify "$1^{commit}" 2>/dev/null
}

if [ -n "$pinned" ]; then
  for ref in "refs/remotes/origin/$pinned" "refs/tags/$pinned" "$pinned"; do
    if commit=$(commit_of "$ref"); then
      echo "$name: $label pinned ref $pinned ($commit)" >&2
      printf '%s\n' "$commit"
      exit 0
    fi
  done
  echo "$name: $label has no ref $pinned" >&2
  exit 1
fi

tries=${SIBLING_REF_TRIES:-30}
interval=${SIBLING_REF_INTERVAL:-20}
attempt=0
while :; do
  if commit=$(commit_of "refs/tags/v$version"); then
    echo "$name: $label tag v$version ($commit)" >&2
    printf '%s\n' "$commit"
    exit 0
  fi
  main=
  for ref in refs/remotes/origin/main refs/heads/main; do
    if main=$(commit_of "$ref"); then break; fi
    main=
  done
  theirs=
  if [ -n "$main" ]; then
    theirs=$(git -C "$dir" show "$main:VERSION" 2>/dev/null | head -n 1 | tr -d '\r') || theirs=
  fi
  if is_version "$theirs" && version_ge "$theirs" "$version"; then
    echo "$name: $label main at $theirs ($main)" >&2
    printf '%s\n' "$main"
    exit 0
  fi
  [ -n "$theirs" ] || theirs="no readable VERSION"
  if [ "$attempt" -ge "$tries" ]; then
    echo "$name: $label has no tag v$version and its main is still at $theirs after $tries fetches;" \
      "push its release (main or the v$version tag) or pin a ref, then rerun" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  echo "$name: $label has no tag v$version and its main is at $theirs; fetching again in $interval s ($attempt of $tries)" >&2
  sleep "$interval"
  if ! git -C "$dir" fetch -q --force origin "+refs/heads/main:refs/remotes/origin/main" "+refs/tags/*:refs/tags/*" </dev/null >/dev/null 2>&1; then
    echo "$name: fetch from $label failed; trying again" >&2
  fi
done
