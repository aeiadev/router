# Contributing

Run the whole suite with `python3 tests/run_all.py` (`python tests/run_all.py` in Git Bash on Windows); it runs every test file and lists every failure.
No test calls a model or the network; CI also checks out the sibling repository and builds the old release trees.

The exception is opt-in: with `SSH_FIXTURE_REQUIRED=1` and `HARNESS_DIR` set, the real SSH tests start the Harness local container, and preparing its pinned image needs a container engine and network access (see `tests/fixtures/operations_ssh/README.md`). The default run skips them and records them as unrun.
Python 3.9 or newer, standard library only.
Keep a change small and add a test that fails without it.

Run one test file with `python3 tests/test_x.py`.
The upgrade and together tests need ROUTER_V01, ROUTER_V02, HARNESS_V01 and HARNESS_V02 set to trees of the published 0.1 and 0.2 commits; the Old release trees step in .github/workflows/ci.yml shows how.
README.md and CHANGELOG.md are edited during the release step.

## Releasing

Update CHANGELOG.md first; the new section starts with the Pairs with line naming the matching version of the sibling tool.
Tag vX.Y.Z on main, then push main and the tag.
Publish the GitHub Release with:

    gh release create vX.Y.Z --title "Router X.Y.Z" --notes "$(scripts/release-notes.sh X.Y.Z)"

Release Router and Harness the same day, Router first.
