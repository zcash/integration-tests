# Cross-Repository CI Integration

This repository supports triggering integration tests from external repositories
via GitHub's [`repository_dispatch`] mechanism. When CI runs on a PR in an
integrated repository, that repository dispatches an event here, which runs the
full integration test suite using the PR's commit. Results are reported back as
status checks on the originating PR.

Integration testing is currently set up for the [`zallet`], [`zebrad`], and
[`zaino`] repositories.

[`repository_dispatch`]: https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#repository_dispatch
[`zallet`]: https://github.com/zcash/zallet
[`zebrad`]: https://github.com/ZcashFoundation/zebra
[`zainod`]: https://github.com/zingolabs/zaino

## How it works

The integration has two sides: the **requesting repository** (e.g.
`ZcashFoundation/zebra`) and this **integration-tests repository**
(`zcash/integration-tests`). Two GitHub Apps are used, one for each direction of
communication:

- **Dispatch app**: This is an app owned by the requesting org that is manually
  installed on `zcash/integration-tests` by repository administrators and is
  used by the requesting repository's CI to trigger workflows here. Each
  requesting organization needs to create its own dispatch app.
- **Status-reporting app** ([`z3-integration-status-reporting`], owned by
  `zcash`, public): Installed on the requesting repository. Used by
  `integration-tests` CI to write status checks back to the requesting
  repository's PR.

[`z3-integration-status-reporting`]: https://github.com/apps/z3-integration-status-reporting

This two-app model follows the principle of least privilege: each app only has
the permissions and credentials for the direction it serves, limiting the blast
radius if either app's credentials are compromised.

### Requesting repository setup

The requesting repository's CI workflow includes a job that dispatches to this
repository:

```yaml
trigger-integration:
  name: Trigger integration tests
  runs-on: ubuntu-latest
  steps:
    - name: Generate app token
      id: app-token
      uses: actions/create-github-app-token@v2
      with:
        app-id: ${{ secrets.DISPATCH_APP_ID }}
        private-key: ${{ secrets.DISPATCH_APP_PRIVATE_KEY }}
        owner: zcash
        repositories: integration-tests
    - name: Trigger integration tests
      env:
        GH_TOKEN: ${{ steps.app-token.outputs.token }}
      run: >
        gh api repos/zcash/integration-tests/dispatches
        --field event_type="<project>-interop-request"
        --field client_payload[sha]="$GITHUB_SHA"
```

This uses the dispatch app's credentials to generate a token scoped to
`zcash/integration-tests`, then sends a `repository_dispatch` event. The
`owner` field must be `zcash` because the token needs write access to the
`zcash/integration-tests` repository.

The `client_payload` must include:

- **`sha`**: The full, 40-hex-digit commit SHA to build and test from the requesting
  repository. This remains authoritative for that repository's binary.

It may also include these optional fields:

- **`platforms`**: A JSON array of platform names (e.g. `["ubuntu-22.04",
  "mingw32"]`) to run only a subset of platforms. Requested platforms are treated
  as required (must pass). The valid list of platform names is maintained
  [here](platform-support). Unrecognized platform names are reported as error
  statuses on the requesting PR. When omitted, all platforms run.
- **`test_sha`**: A commit SHA or ref in the `zcash/integration-tests` repository
  itself. Setup checks it out once and passes the resolved commit to all build
  and test jobs. Tests, source resolution, build scripts, and composite actions
  therefore use the same revision, even if the supplied branch moves. This does
  not replace the workflow definitions that GitHub executes.
- **`zebra_sha`**, **`zaino_sha`**, **`zallet_sha`**: Companion source overrides in
  `ZcashFoundation/zebra`, `zingolabs/zaino`, and `zcash/zallet`, respectively.
  Each accepts a full commit SHA or a named branch/tag. Use `refs/heads/...` or
  `refs/tags/...` if a branch and tag share a name. Abbreviated commit SHAs and
  revision expressions are not supported. The field for the requesting project
  is ignored: for example, a `zebra-interop-request` always builds `sha`, even
  when `zebra_sha` is also supplied.

#### Pairing companion sources

For each project, setup selects the requester's `sha` first, otherwise its
companion override, otherwise the compatible cohort pinned in
`.github/scripts/resolve_source_refs.py`. The initial pre-v29 cohort comes from
[the successful September 30 backend matrix](https://github.com/zcash/integration-tests/actions/runs/36772958143).
Advance these defaults together only after the replacement cohort passes the
backend matrix; do not change them back to moving branches.

Setup resolves selected named refs once to full SHAs and logs the resulting
cohort. All required and extra build platforms consume those same immutable
source revisions. A dispatch still tests the requester's actual commit, not
the pinned fallback for that project.

For example, after publishing compatible reader commits, dispatch a Zebra
writer together with those companions (the environment variables must contain
the actual published revisions):

```sh
gh api repos/zcash/integration-tests/dispatches \
  --field event_type=zebra-interop-request \
  --field "client_payload[sha]=$ZEBRA_SHA" \
  --field "client_payload[zaino_sha]=$ZAINO_SHA" \
  --field "client_payload[zallet_sha]=$ZALLET_SHA"
```

These overrides select source, not dependency rewrites. Every build uses
`cargo build --locked`, including the Zallet launcher and both backend
workspaces. Companion commits must already contain coherent manifests and
committed lockfiles for the tested writer. CI does not patch Cargo dependencies,
replace SDK pins, or regenerate locks.

In particular, pairing a v29 Zebra writer requires readers built with its
compatible Zebra state implementation; merely rebuilding an unchanged v28
reader cannot make it read v29. Zallet also needs a coherent SDK cohort across
all three workspaces and a compatible **embedded** Zaino revision in its
backend manifests and lockfile. `zaino_sha` selects the standalone `zainod`,
not that embedded dependency. A compatible published source graph is a
prerequisite, not something this workflow creates.

The pre-v29 defaults restore ordinary integration-tests runs; they do not make
a NU7 Zebra dispatch compatible with the old readers. Such dispatches must
supply compatible reader revisions. Reader upgrades and a passing v29 cohort
remain tracked by [#206](https://github.com/zcash/integration-tests/issues/206).

Final-binary cache keys include only the binary's own source SHA, platform,
and build recipe. The recipe hashes the executing `build-binary.yml` workflow,
the selected Zallet build scripts when applicable, and actual build arguments
and platform properties. It excludes source-selection code, `ci.yml`, shard
counts, test runners, and required/optional job labels. Changing only the Zebra
writer does not rebuild unchanged Zaino or Zallet binaries.

Build scripts are checked out at setup's resolved integration-tests commit.
A separate sparse checkout at `github.workflow_sha` supplies the executing
workflow for recipe hashing, so a `test_sha` override cannot make the recipe
hash describe a workflow that GitHub did not execute.

Cargo caches use stable per-platform, per-binary keys. The cache action tracks
toolchain and dependency inputs and can partially restore across source and
lockfile changes; Cargo determines what must rebuild. Final binaries still
require an exact cache match. Neither cache key establishes database
compatibility: paired startup and RPC tests must pass.

[platform-support]: https://zcash.github.io/integration-tests/user/platform-support.html

### Integration-tests repository setup

In the integration-tests repository CI, three things are configured:

1. **`ci.yml` trigger**: The workflow's `on.repository_dispatch.types` array
   includes the event type (`zebra-interop-request`, `zallet-interop-request`,
   and `zaino-interop-request` are currently supported).

2. **Source selection and build jobs**: `ci.yml` setup uses
   `.github/scripts/resolve_source_refs.py` to select and resolve all three
   sources, taking dispatch fields through environment variables. Each of the
   six reusable `build-binary.yml` calls receives its immutable `source_sha`
   and setup's resolved `test_sha`; build jobs do not resolve moving refs again.
   The resolver's offline regression can be run with
   `python3 .github/scripts/test_resolve_source_refs.py`.

3. **Status reporting**: Four composite actions in `.github/actions/` handle
   communication back to the requesting repository:
   - `interop-repo-ids` maps the event type (e.g. `zebra-interop-request`) to
     the requesting repository's owner and name. This mapping is maintained as
     a `case` expression so that only known event types resolve to valid
     repositories.
   - `start-interop` generates a token from the
     `z3-integration-status-reporting` app scoped to the requesting repository
     and creates a **pending** status check on the dispatched commit.
   - `finish-interop` (run with `if: always()`) updates that status to the
     job's final result (success, failure, or error).
   - `notify-interop-error` reports error statuses for any requested platforms
     that are not recognized by the CI matrix (see `platforms` in
     [client_payload](#requesting-repository-side)).

   Each job calls `interop-repo-ids` first, then passes its outputs to
   `start-interop` at the beginning and `finish-interop` at the end.

## Security model

**Who can trigger integration test workflows?** Two independent gates control
this:

1. **App installation on `zcash/integration-tests`**: The dispatch app must be
   installed on `zcash/integration-tests`, which requires approval from a
   `zcash` org admin. For `zcash`-internal repos, the org-private
   [`z3-integration-dispatch`] app is used, so no external organization can use
   it. For external repos, the requesting organization creates its own dispatch
   app, which a `zcash` admin must explicitly approve for installation.
2. **Event type allowlist in `ci.yml`**: The workflow only responds to event
   types explicitly listed in `on.repository_dispatch.types`. Even if an app
   could dispatch an event, it would be ignored unless its type is listed.

**Credential separation**: Each app's private key is stored only where it is
needed — the dispatch app's key in the requesting repository, the
`z3-integration-status-reporting` key (a single key pair, since it is one app)
in `zcash/integration-tests`. If a dispatch app's credentials are compromised,
an attacker could trigger integration test runs but could not write arbitrary
statuses. If the
status-reporting credentials are compromised, an attacker could write status
checks to repositories where the app is installed but could not trigger workflow
runs.

**Token downscoping**: When generating installation tokens from the
status-reporting app, the composite actions use the `permission-statuses: write`
parameter of [`actions/create-github-app-token`] to restrict each token to only
the `statuses` permission. Even if the app's installation were to have broader
permissions approved in the future, the tokens generated by the CI workflows
would still be limited to writing commit statuses.

[`actions/create-github-app-token`]: https://github.com/actions/create-github-app-token

**App permissions**:

| App | Permission | Purpose |
|-----|-----------|---------|
| `z3-integration-dispatch` | `Contents`: Read and write | Send `repository_dispatch` events to `zcash/integration-tests` |
| `z3-integration-status-reporting` | `Statuses`: Read and write | Create and update commit status checks on requesting repos |

## Setting up integration for a new repository

To add cross-repository integration for a new project, follow these steps. The
instructions below use Zebra (`ZcashFoundation/zebra`) as a running example.

### 1. Set up the dispatch app (requesting org side)

The requesting repository needs a dispatch app installed on
`zcash/integration-tests`.

The requesting organization creates a GitHub App with:

- **Repository permissions**: `Contents`: Read and write

Then have a `zcash` org admin install it on `zcash/integration-tests`. Store the
app's ID and private key as repository secrets in the requesting repository
(as `DISPATCH_APP_ID` and `DISPATCH_APP_PRIVATE_KEY`) so that they can be used
for CI configuration as described in the next step.

### 2. Update the requesting repository's CI

Add a `trigger-integration` job to the requesting repository's CI workflow (see
the [example above](#requesting-repository-side)). Use the event type
`<project>-interop-request` appropriate to the requesting repository where
`<project>` is one of `zallet`, `zebra`, or `zaino`.

### 3. Install `z3-integration-status-reporting` on the requesting repository

The [`z3-integration-status-reporting`] app must be installed on the requesting
repository so that `integration-tests` can write status updates back to the
PR or workflow run that triggered the test.

An admin of the requesting organization can install the app via
https://github.com/apps/z3-integration-status-reporting/installations/new

During installation, select only the specific repository that needs integration
(e.g. `zebra`).

### 4. Verify

Open a test PR in the requesting repository and confirm that:
- The `trigger-integration` job dispatches successfully.
- Integration tests run in this repository.
- Status checks appear on the requesting repository's PR commit.
- Both success and failure states propagate correctly.
