#!/usr/bin/env python3
"""Run with python3; exercise selection and real git refs without network access."""

import subprocess
import tempfile

from resolve_source_refs import SOURCES, full_sha, resolve_ref, select_ref


def rejects(function, *args):
    try:
        function(*args)
    except (ValueError, subprocess.CalledProcessError):
        return
    raise AssertionError(f"Accepted invalid input: {args!r}")


def main():
    with tempfile.TemporaryDirectory() as remote:

        def git(*args):
            return subprocess.check_output(
                [
                    "git",
                    "-C",
                    remote,
                    "-c",
                    "user.name=Source selection regression",
                    "-c",
                    "user.email=source-selection@example.invalid",
                    *args,
                ],
                text=True,
            ).strip()

        git("init", "--initial-branch=main")
        git(
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "First source",
        )
        first = git("rev-parse", "HEAD")
        git("-c", "tag.gpgsign=false", "tag", "-a", "v1", "-m", "Pinned source")
        git(
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Next source"
        )
        second = git("rev-parse", "HEAD")
        # A valid ref may contain shell metacharacters; it must remain a literal argument.
        git("branch", "pairing/nu7;literal")
        git("branch", "v1")

        for project, (_, default_ref) in SOURCES.items():
            assert (
                full_sha(default_ref) == default_ref
            )  # Defaults must not track moving refs.
            assert select_ref(project, default_ref, "", "", "") == default_ref
            assert (
                select_ref(project, default_ref, "", "", "pairing/nu7") == "pairing/nu7"
            )
            for requester in SOURCES:
                selected = select_ref(
                    project, default_ref, f"{requester}-interop-request", first, second
                )
                assert selected == (first if project == requester else second)
            rejects(
                select_ref,
                project,
                default_ref,
                f"{project}-interop-request",
                "",
                second,
            )
            rejects(
                select_ref,
                project,
                default_ref,
                f"{project}-interop-request",
                "main",
                second,
            )

        assert full_sha(first.upper()) == first
        for invalid in ("", first[:-1], first + "0", "g" * 40, first + "\n"):
            rejects(full_sha, invalid)
        assert resolve_ref(remote, first) == first
        assert resolve_ref(remote, "main") == second
        assert resolve_ref(remote, "refs/heads/main") == second
        assert resolve_ref(remote, "pairing/nu7;literal") == second
        assert resolve_ref(remote, "refs/tags/v1") == first
        assert resolve_ref(remote, "refs/heads/v1") == second
        rejects(
            resolve_ref, remote, "v1"
        )  # Ambiguous branch/tag, not an arbitrary choice.
        for invalid in (
            "missing",
            "main*",
            "main?",
            "main\n",
            "../main",
            "main^{commit}",
        ):
            rejects(resolve_ref, remote, invalid)

    print("Source selection, precedence, validation, and git ref resolution passed")


if __name__ == "__main__":
    main()
