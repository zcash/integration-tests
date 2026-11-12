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

- **`sha`**: The commit SHA to build and test from the requesting repository.

It may also include these optional fields:

- **`platforms`**: A JSON array of platform names (e.g. `["ubuntu-22.04",
  "mingw32"]`) to run only a subset of platforms. Requested platforms are treated
  as required (must pass). The valid list of platform names is maintained
  [here](platform-support). Unrecognized platform names are reported as error
  statuses on the requesting PR. When omitted, all platforms run.
- **`test_sha`**: A commit SHA or ref in the `zcash/integration-tests` repository
  itself. When provided, the test suite is checked out at this ref instead of
  `main`. This is useful for testing integration-tests changes alongside project
  changes.
- **`source_ref`**: A stable identifier for the source of the request (e.g.
  `pr-42` for a pull request, or a branch name such as `main`). It is used only
  as the [concurrency](https://docs.github.com/actions/using-jobs/using-concurrency)
  group key, so that a rapid follow-up push for the same source PR cancels the
  superseded interop run. When omitted, the run groups by `sha` instead, which
  is unique per push and so never cancels a prior run.

[platform-support]: https://zcash.github.io/integration-tests/user/platform-support.html

### Integration-tests repository setup

In the integration-tests repository CI, three things are configured:

1. **`ci.yml` trigger**: The workflow's `on.repository_dispatch.types` array
   includes the event type (`zebra-interop-request`, `zallet-interop-request`,
   and `zaino-interop-request` are currently supported).

2. **Build job**: A build job checks out the requesting repository at the
   dispatched commit SHA:
   ```yaml
   - name: Use specified commit
     if: github.event.action == '<project>-interop-request'
     env:
       SHA: ${{ github.event.client_payload.sha }}
     run: echo "PROJECT_REF=${SHA}" >> $GITHUB_ENV

   - name: Use current main
     if: github.event.action != '<project>-interop-request'
     run: echo "PROJECT_REF=refs/heads/main" >> $GITHUB_ENV
   ```

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
