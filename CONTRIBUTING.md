# Contributing

Run the checks in .github/workflows/ci.yml; none of them calls a model or the network.
Python 3.10 or newer, standard library only.
Keep a change small and add a test that fails without it.

Run one test with `python3 tests/test_x.py`.
The install tests need old release trees built from the published commits and tags; CI prepares them.
README.md and CHANGELOG.md are edited during the release step.

## Releasing

Update CHANGELOG.md first; the new section starts with the Pairs with line naming the matching version of the sibling tool.
Tag vX.Y.Z on main, then push main and the tag.
Publish the GitHub Release with:

    gh release create vX.Y.Z --title "Router X.Y.Z" --notes "$(scripts/release-notes.sh X.Y.Z)"

Release Router and Harness the same day, Router first.
