#!/usr/bin/env python3
"""Select the dispatched source cohort and resolve it once before the build matrix."""

import os
import re
import subprocess
import sys


SOURCES = {
    "zebra": ("ZcashFoundation/zebra", "refs/heads/main"),
    "zaino": ("zingolabs/zaino", "refs/heads/dev"),
    "zallet": ("zcash/zallet", "refs/heads/main"),
}


def full_sha(value):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError(f"Expected a full commit SHA, got {value!r}")
    return value.lower()


def select_ref(project, default_ref, action, own_sha, companion_ref):
    # The requester owns its binary's identity, even if its companion field is set.
    if action == f"{project}-interop-request":
        return full_sha(own_sha)
    return companion_ref or default_ref


def resolve_ref(remote, ref):
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return full_sha(ref)

    # Only literal git refs are accepted, not ls-remote patterns or revision syntax.
    candidates = [ref] if ref.startswith("refs/") else [f"refs/heads/{ref}", f"refs/tags/{ref}"]
    for candidate in candidates:
        subprocess.run(["git", "check-ref-format", candidate], check=True, timeout=10)
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", remote,
         *candidates, *(f"{candidate}^{{}}" for candidate in candidates)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    advertised = dict(line.split("\t", 1)[::-1] for line in result.stdout.splitlines())
    matches = [candidate for candidate in candidates if candidate in advertised]
    if len(matches) != 1:
        raise ValueError(f"Expected one ref for {ref!r}; use refs/heads/ or refs/tags/ to disambiguate")
    candidate = matches[0]
    # An annotated tag names a tag object; builds need its peeled commit instead.
    return full_sha(advertised.get(f"{candidate}^{{}}", advertised[candidate]))


def main():
    resolved = {}
    for project, (repository, default_ref) in SOURCES.items():
        ref = select_ref(
            project, default_ref, os.environ.get("INTEROP_ACTION", ""),
            os.environ.get("REQUEST_SHA", ""), os.environ.get(f"{project.upper()}_REF", ""),
        )
        sha = resolve_ref(f"https://github.com/{repository}.git", ref)
        print(f"Resolved {repository} {ref!r} to {sha}", file=sys.stderr)
        resolved[project] = sha
    for project, sha in resolved.items():
        print(f"{project}_sha={sha}")


if __name__ == "__main__":
    main()
