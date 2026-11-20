#!/usr/bin/env python3
#
# Decide whether a PR only touches paths that have no effect on the CI
# workflow, so that `ci-skip.yml` can report the required status as passed
# without running CI.
#
# Reads the changed file names (one per line, as printed by
# `git diff --name-only`) on stdin and prints a GitHub Actions output line:
#
#   result=verified   every changed file matches NO_CI_PATHS
#   result=skipped    at least one changed file needs CI
#
# Exits with status 1 if no files were read.
#
# NO_CI_PATHS must stay a superset of the `paths` filter of the
# `pull_request_target` trigger in `ci-skip.yml`, and of the `paths-ignore`
# filters in `ci.yml`.
#
# `ci-skip.yml` runs on `pull_request_target`, so this script is always the
# copy from the base branch, never the one from the PR.

import re
import shlex
import sys
from collections.abc import Iterable
from typing import TextIO

NO_CI_PATHS = [
    r"^\.github/dependabot\.yml$",
    r"^\.github/workflows/audits\.yml$",
    r"^\.github/workflows/book\.yml$",
    r"^\.github/workflows/ci-skip\.yml$",
    r"^\.github/workflows/lints\.yml$",
    r"^\.github/workflows/release-docker-hub\.yml$",
    r"^contrib/debian/copyright$",
    r"^doc/.*",
    r".*\.md$",
    r"^COPYING$",
    r"^INSTALL$",
]
NO_CI_PATHS_REGEX = re.compile("(?:{})".format("|".join(NO_CI_PATHS)))


def read_changed_files(stream: TextIO) -> list[str]:
    """Split the input on newlines only, undoing git's path quoting."""
    lex = shlex.shlex(stream, posix=True)
    lex.whitespace = "\n\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def no_ci_required(changed_files: Iterable[str]) -> bool:
    """True when every changed file is one CI does not depend on."""
    return all(NO_CI_PATHS_REGEX.match(f) for f in changed_files)


def main() -> None:
    changed_files = read_changed_files(sys.stdin)
    if len(changed_files) == 0:
        sys.exit(1)
    verified = no_ci_required(changed_files)
    print("result=verified" if verified else "result=skipped")


if __name__ == "__main__":
    main()
