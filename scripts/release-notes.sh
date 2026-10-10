#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 1 ]; then
  printf "%s\n" "usage: release-notes.sh VERSION [CHANGELOG]" >&2
  exit 2
fi
version=$1
changelog=${2:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/CHANGELOG.md"}
if [ ! -r "$changelog" ]; then
  printf "release-notes: %s: cannot read\n" "$changelog" >&2
  exit 1
fi
if ! RELEASE_NOTES_VERSION="$version" awk '
  BEGIN {heading="## " ENVIRON["RELEASE_NOTES_VERSION"]}
  $0 == heading {found=1; active=1; next}
  active && /^## / {active=0}
  active {if (count || $0 != "") {line[++count]=$0; if ($0 != "") last=count}}
  END {if (found) {for (i=1; i<=last; i++) print line[i]} else exit 1}
' "$changelog"; then
  printf "release-notes: no section for %s\n" "$version" >&2
  exit 1
fi
