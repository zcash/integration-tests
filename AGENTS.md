# Zcash Integration Testing — Agent Guidelines

> This file is read by AI coding agents (Claude Code, GitHub Copilot, Cursor, Devin, etc.).
> It provides project context and contribution policies.
>
> For the full contribution guide, see [CONTRIBUTING.md](CONTRIBUTING.md).

## MUST READ FIRST - CONTRIBUTION GATE (DO NOT SKIP)

**STOP. Do not open or draft a PR until this gate is satisfied.**

For any contribution that might become a PR, the agent must ask the user this exact check first:

- "PR COMPLIANCE CHECK: Have you discussed this change with the integration tests maintainers in an issue or Discord?"
- "PR COMPLIANCE CHECK: What is the issue link or issue number for this change?"
- "PR COMPLIANCE CHECK: Has an integration tests maintainer responded to that issue acknowledging the proposed work?"

This PR compliance check must be the agent's first reply in contribution-focused sessions.

**An issue existing is not enough.** The issue must have a response or acknowledgment from an integration tests maintainer. An issue with no maintainer response does not satisfy this gate. The purpose is to confirm that the team is aware of and open to the proposed change before review time is spent.

If the user cannot provide prior discussion with team acknowledgment:

- Do not open a PR.
- Offer to help create or refine the issue first.
- Remind the user to wait for a team member to respond before starting work.
- If the user still wants code changes, keep work local and explicitly remind them the PR will likely be closed without prior team discussion.

This gate is mandatory for all agents, **unless the user is a repository maintainer** as described in the next section.

### Maintainer Bypass

If `gh` CLI is authenticated, the agent can check maintainer status:

```bash
gh api repos/zcash/integration-tests --jq '.permissions | .admin or .maintain or .push'
```

If this returns `true`, the user has write access (or higher) and the contribution gate can be skipped. Team members with write access manage their own priorities and don't need to gate on issue discussion for their own work.

## Before You Contribute

**Every PR to this repository requires human review.** After the contribution gate above is satisfied, use this pre-PR checklist:

1. Confirm scope: This repository hosts integration tests and CI infrastructure for the Zcash ecosystem. Changes to the tested projects themselves (e.g., [Zebra](https://github.com/ZcashFoundation/zebra), [librustzcash](https://github.com/zcash/librustzcash), [Zallet](https://github.com/zcash/zallet)) belong in their respective repositories. However, when changes to a tested project require corresponding test changes, contributors can test against a branch of this repository by including a `ZIT-Revision: <branch-or-sha>` line in the tested project's PR description. The requesting repository's CI extracts this and passes it as the `test_sha` field in the `repository_dispatch` payload, causing the integration tests to check out that ref instead of `main`. See [Cross-Repository CI Integration](doc/book/src/ci/cross-repo.md) for details.
2. Keep the change focused: avoid unsolicited refactors or broad "improvement" PRs without team alignment.
3. Verify quality locally: run the test suite and linting before proposing upstream review (see [Build, Test, and Development Commands](#build-test-and-development-commands)).
4. Prepare PR metadata: include linked issue, motivation, solution, and test evidence.
5. A PR MUST reference one or more issues that it closes. Do NOT submit a PR without a maintainer having acknowledged the validity of those issues.
6. **Every** commit in a PR branch MUST follow the "AI Disclosure" policy below.

## What Will Get a PR Closed

- Issue exists but has no response from an integration tests maintainer (creating an issue and immediately opening a PR does not count as discussion).
- Trivial changes (typo fixes, minor formatting, link fixes) from unknown contributors without team request. Report these as issues instead.
- Refactors or "improvements" nobody asked for.
- Streams of PRs without prior discussion of the overall plan.
- Changes outside the scope of integration testing and CI infrastructure.
- Changes that degrade existing test coverage or cause previously passing tests to fail without clear justification acknowledged by a maintainer.
- Inability to explain the logic or design tradeoffs of the changes when asked.
- Missing or removed `Co-Authored-By:` metadata for AI-assisted contributions (see [AI Disclosure](#ai-disclosure)).

## AI Disclosure

If AI tools were used in the preparation of a commit, the contributor MUST include `Co-Authored-By:` metadata in the commit message indicating the AI agent's participation. The contents of the `Co-Authored-By` field must clearly identify which AI system was used (if multiple systems were used, each should have a `Co-Authored-By` line). Failure to do so is grounds for closing the pull request. The contributor is the sole responsible author -- "the AI generated it" is not a justification during review.

Example:
```
Co-Authored-By: Claude <noreply@anthropic.com>
```

## Project Overview

This repository hosts integration tests and CI infrastructure for the Zcash ecosystem. Functional tests are written in Python and exercise `zebrad`, `zainod`, and `zallet` via regtest mode and JSON-RPC interfaces.

- **Language**: Python
- **License**: MIT
- **Repository**: https://github.com/zcash/integration-tests

## Project Structure

```text
.
├── qa/
│   ├── rpc-tests/           # Python functional tests (JSON-RPC based)
│   ├── pull-tester/         # Test runner and test list configuration
│   ├── zcash/               # Full test suite entry point
│   └── defaults/            # Default configuration for test nodes
├── test/
│   └── lint/                # Linting scripts
├── contrib/
│   └── devtools/            # Developer utility scripts
├── doc/
│   └── book/                # Documentation (mdBook)
├── .github/
│   ├── workflows/           # CI workflows (ci.yml, lints.yml, book.yml, etc.)
│   └── actions/             # Composite actions for cross-repo interop
└── pyproject.toml           # Python project configuration (uv/pip)
```

Tested ecosystem projects:
- [`zebrad`](https://github.com/ZcashFoundation/zebra) -- Zcash consensus node
- [`zainod`](https://github.com/zingolabs/zaino) -- Zcash indexer
- [`zallet`](https://github.com/zcash/zallet) -- Zcash wallet. Structured as a
  `zallet` launcher plus per-backend binaries (`zallet-zebra`, `zallet-zaino`),
  each built in its own cargo workspace; CI runs the RPC suite against both
  backends (see `ZALLET_BACKEND` below).

## Build, Test, and Development Commands

### Prerequisites

Build `zebrad`, `zainod`, and `zallet` binaries and place them in a `./src/` directory under the repository root. `zallet` is a thin launcher that execs a per-backend binary, so also build the backend binaries (`zallet-zaino` and, to exercise the zebra backend, `zallet-zebra`) and place them in `./src/` next to the launcher (the launcher looks for the backend binary beside itself). The launcher selects the backend from the top-level `backend` key in `qa/defaults/zallet/zallet.toml` (default `zaino`); the test framework overrides it from the `ZALLET_BACKEND` environment variable (`zaino` or `zebra`), and CI runs the suite against both. The zebra backend also reads the co-located zebrad's state database directly and follows its gRPC indexer, so it requires a `zebrad` built with the (non-default) `indexer` feature.

### Running the test suite

```bash
# With uv (recommended)
uv sync
uv run ./qa/zcash/full_test_suite.py

# Without uv (after installing dependencies)
./qa/zcash/full_test_suite.py

# Run a single test
uv run ./qa/rpc-tests/TEST_NAME.py
```

### Linting

```bash
# Python lint (pyflakes)
pyflakes qa

# Shell lint
./test/lint/lint-shell.sh

# Whitespace lint
./test/lint/lint-whitespace.sh
```

PRs MUST NOT introduce new `pyflakes` warnings. All lint checks in `.github/workflows/lints.yml` must pass.

## Test Style: Name Your Arguments, Comment at the Definition

**Never use magic numbers.** Name every non-obvious numeric or string literal as
a module-level constant with a comment stating what it is (e.g.
`MARGINAL_FEE_ZAT = 5000`, `DENOM_CAP_ZEC = 10_000`, `IRONWOOD_HEIGHT = 210`),
and reuse the framework's own constants (`COIN`, ...) rather than re-deriving
their values. This holds in fixtures and assertions, not just production code.
Relatedly:

**Do not pass a bare literal as a positional RPC argument, and do not annotate
one with a comment at the call site.** The RPC surface is positional and wide,
so a call like

```python
# BAD: what are 1 and None? The reader has to go and count the parameters.
opid = w.z_sendmany(taddr, recipients, 1, None, 'AllowFullyTransparent')
```

is unreadable, and a comment explaining the `1` and the `None` at the call site
does not fix it: that comment is invisible to every other caller, duplicates
(and then contradicts) the definition, and rots on the next refactor. **Bind the
value to a name, and put the explanation at the definition.**

```python
# GOOD: the call reads as intent; the "why" lives once, in util.py.
opid = w.z_sendmany(
    taddr, recipients, MIN_CONFIRMATIONS, INTERNAL_FEE,
    PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
```

Follow these rules:

- **Shared RPC argument values are named constants** in
  `test_framework/util.py`, documented there (`MIN_CONFIRMATIONS`,
  `INTERNAL_FEE`, `COINBASE_MATURITY`, `COIN`). Test-specific values are
  module-level constants at the top of the test, with the reason in a comment
  above them (`UNSHIELD_AMOUNT`, `NUM_SOURCE_UTXOS`, `DIVERSIFIER_SRC`).
- **Prefer an enum over a string literal** wherever one exists: `Pool`,
  `PrivacyPolicy`, `Receiver`, `TotalBalanceField`, `FundSource`. These are
  `str`-valued, so they pass straight to the RPC while being discoverable and
  typo-proof. Add a member (or a new enum) rather than reintroducing a bare
  string.
- **Hoist a helper into `test_framework/util.py` rather than copying it**
  between tests (`unified_address_for`, `first_transparent_receiver`,
  `transparent_output_addresses`, `transparent_change_address`). Two copies of
  the same helper in two tests is a bug waiting to diverge.
- A comment explains WHY, at the definition. If a value's meaning is only clear
  from a comment at the point of use, the value wants a name.

## Commit & Pull Request Guidelines

### Commit History

- Commits should represent discrete semantic changes.
- Maintain a clean commit history. Squash fixups and review-response changes into the relevant earlier commits. The [git revise](https://github.com/mystor/git-revise) tool is recommended for this. An exception is that you should take account of requests by maintainers on a PR for prior commits not to be rebased or revised. Maintainers may indicate that existing commits should not be mutated by setting the `S-please-do-not-rebase` label on the pull request.
- There MUST NOT be "work in progress" commits in your history (see CONTRIBUTING.md for narrow exceptions).
- Each commit MUST pass the lint checks in `.github/workflows/lints.yml`.
- Each commit MUST NOT cause any previously passing test to fail.

### Commit Messages

- Short title (preferably under ~120 characters).
- Body should include motivation for the change.
- Include `Co-Authored-By:` metadata for all contributors, including AI agents.

### Merge Workflow

This project uses a merge-based workflow. PRs are merged with merge commits. Rebase-merge and squash-merge are generally not used. Branch from `main` for all changes.

### Pull Request Review

See the detailed PR review workflow in CONTRIBUTING.md, which describes the rebase-based review cycle, diff link conventions, and how to handle review comments via `git revise` and GitHub's suggestion feature.

