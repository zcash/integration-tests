# CI Infrastructure

This repository's CI pipeline builds and tests the Z3 stack components
([`zebrad`], [`zainod`], and [`zallet`]) together as an integrated system.

[`zebrad`]: https://github.com/ZcashFoundation/zebra
[`zainod`]: https://github.com/zingolabs/zaino
[`zallet`]: https://github.com/zcash/zallet

## What CI does

On every pull request and push to `main`, CI:

1. **Builds** the `zebrad`, `zainod`, and `zallet` binaries from their latest
   released versions (or from a specific commit when triggered by a cross-repo
   dispatch).
2. **Runs the RPC test suite** against the built binaries on required platforms.
3. **Reports results** back to the triggering repository when invoked via
   cross-repo dispatch.

## Platform matrix

| Platform | Required | Tests |
|----------|----------|-------|
| Ubuntu 22.04 (x86_64) | Yes | Build + RPC tests |
| Ubuntu 24.04 (x86_64) | No | Build only |
| Windows (64-bit MinGW) | No | Build only |
| ARM64 Linux | No | Build only |

Required platforms must pass for CI to succeed. RPC tests are only run on
required platforms to manage CI costs. When CI is triggered via cross-repo
dispatch, callers may specify which platforms to run; all explicitly requested
platforms are treated as required.

## Cross-repository integration

External repositories can trigger integration tests from their PRs and receive
results back as status checks. See [Cross-Repository CI](cross-repo.md) for
the mechanism and setup instructions.
