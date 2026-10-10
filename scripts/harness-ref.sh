#!/usr/bin/env bash
# usage: harness-ref.sh HARNESS_DIR VERSION_FILE
# Prints the newest release tag vMAJOR.MINOR.PATCH (digits only) of HARNESS_DIR
# whose MAJOR.MINOR equals the one in VERSION_FILE. Prints nothing when none.
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: harness-ref.sh HARNESS_DIR VERSION_FILE" >&2
  exit 2
fi
dir=$1
file=$2
version=$(head -n 1 "$file") || exit 1
if ! printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "harness-ref: cannot read a version from $file" >&2
  exit 1
fi
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
tags=$(git -C "$dir" tag --list 'v*') || exit 1
printf '%s\n' "$tags" \
  | grep -E "^v${major}\\.${minor}\\.[0-9]+\$" \
  | sort -t. -k3,3n \
  | tail -n 1 || true
