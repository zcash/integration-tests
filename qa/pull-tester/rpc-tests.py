#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2020-2022 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .
"""
rpc-tests.py - run regression test suite

This module calls down into individual test cases via subprocess. It will
forward all unrecognized arguments onto the individual test scripts.

RPC tests are disabled on Windows by default. Use --force to run them anyway.

For a description of arguments recognized by test scripts, see
`qa/pull-tester/test_framework/test_framework.py:BitcoinTestFramework.main`.

"""

import argparse
import configparser
import os
import time
import shutil
import sys
import subprocess
import tempfile
import re

SERIAL_SCRIPTS = [
    # These tests involve enough shielded spends (consuming all CPU
    # cores) that we can't run them in parallel. The runner starts them
    # on their own, and CI gives each one its own shard.
]

FLAKY_SCRIPTS = [
    # These tests have intermittent failures that we haven't diagnosed yet.
    # CI runs each on its own shard so it need not be a required check.
]

# Tests that target the Z3 stack but are blocked on a specific upstream bug.
# Note = what they are waiting on. This is deliberately short: tests that were
# merely never ported from zcashd were removed when zcashd reached end of life,
# and what they covered is recorded in doc/book/src/dev/migration-backlog.md.
DISABLED_SCRIPTS = [
    'addnode.py',  # zebra regtest peering stalls in multi-peer topologies (zebra #10329, #10332)
    # zebra-backend: wait_for_wallet_sync never converges after invalidateblock,
    # and a recovered account's wait_for_wallet_sync(timeout=300) times out
    # (still hangs as of zallet@d168efe, past zallet#560/#563/#576).
    'wallet_ironwood_reorg.py',
    'wallet_ironwood_birthday.py',
    # migrate-zcashd-wallet aborts on a librustzcash regression: a legacy standalone
    # transparent key whose address is already an account-derived receiver re-inserts
    # that address, violating the UNIQUE index on
    # addresses.cached_transparent_receiver_address (added in librustzcash ef6214bc5e).
    # Re-enable once the standalone-import path resolves the collision (the
    # import-direction counterpart of librustzcash 3b6a45575c).
    'zcashd_key_import.py',
    'zcashd_key_import_db.py',
]

BASE_SCRIPTS = [
    # Longest test should go first, to favor running tests in parallel
    # vv Tests less than 10m vv
    # These Ironwood tests span the NU6.3 activation boundary or chain many
    # cross-pool sends, so they mine several coinbase-maturity windows.
    'wallet_ironwood_orchard_turnstile.py',
    'wallet_ironwood_activation.py',
    # vv Tests less than 7m vv
    # The two-shield Ironwood tests each mine two coinbase-maturity windows.
    'wallet_ironwood_crosspool.py',
    'wallet_ironwood_spending.py',
    'wallet_ironwood_invariants.py',
    # vv Tests less than 5m vv
    'wallet.py',
    'wallet_ironwood_persistence.py',
    'wallet_ironwood_views.py',
    'wallet_ironwood_conservation.py',
    'wallet_ironwood_negatives.py',
    # vv Tests less than 2m vv
    'wallet_ironwood.py',
    'wallet_ironwood_reorg.py',
    'wallet_ironwood_birthday.py',
    'wallet_changeaddresses.py',
    'wallet_legacy_pool_spend.py',
    # vv Tests less than 60s vv
    'addnode.py',
    'wallet_z_shieldcoinbase.py',
    'wallet_z_shieldcoinbase_multi_taddr.py',
    'wallet_transparent_spend.py',
    'wallet_z_importviewingkey.py',
    'regtest_signrawtransaction.py',
    # vv Tests less than 30s vv
    'feature_nu6.py',
    'feature_backup_non_finalized_state.py',
    'getrawtransaction_sidechain.py',
    'fix_block_commitments.py',
    'indexer.py',
    'decodescript.py',
    'feature_nu6_1.py',
    'nuparams.py',
    'getmininginfo.py',
    'converttex.py',
    'zcashd_key_import.py',
    'zcashd_key_import_db.py',
]

# ALL_SCRIPTS is left complete so a disabled test can still be run explicitly
# by name; the lists that actually get run have DISABLED_SCRIPTS removed.
# Matching is on the script filename so entries with extra args (e.g.
# 'foo.py --bar') are covered too.
ALL_SCRIPTS = SERIAL_SCRIPTS + FLAKY_SCRIPTS + BASE_SCRIPTS

_DISABLED_FILES = {s.split()[0] for s in DISABLED_SCRIPTS}
def _without_disabled(scripts):
    return [s for s in scripts if s.split()[0] not in _DISABLED_FILES]
SERIAL_SCRIPTS = _without_disabled(SERIAL_SCRIPTS)
FLAKY_SCRIPTS = _without_disabled(FLAKY_SCRIPTS)
BASE_SCRIPTS = _without_disabled(BASE_SCRIPTS)


def main():
    # Parse arguments and pass through unrecognised args
    parser = argparse.ArgumentParser(add_help=False,
                                     usage='%(prog)s [rpc-test.py options] [script options] [scripts]',
                                     description=__doc__,
                                     epilog='''
    Help text and arguments for individual test script:''',
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--coverage', action='store_true', help='generate a basic coverage report for the RPC interface')
    parser.add_argument('--deterministic', '-d', action='store_true', help='make the output a bit closer to deterministic in order to compare runs.')
    parser.add_argument('--exclude', '-x', help='specify a comma-separated-list of scripts to exclude. Do not include the .py extension in the name.')
    parser.add_argument('--force', '-f', action='store_true', help='run tests even on platforms where they are disabled by default (e.g. windows).')
    parser.add_argument('--help', '-h', '-?', action='store_true', help='print help text and exit')
    parser.add_argument('--jobs', '-j', type=int, default=4, help='how many test scripts to run in parallel. Default=4.')
    parser.add_argument('--machines', '-m', type=int, default=-1, help='how many machines to shard the tests over. must also provide individual shard index. Default=-1 (no sharding).')
    parser.add_argument('--rpcgroup', '-r', type=int, default=-1, help='individual shard index. must also provide how many machines to shard the tests over. Default=-1 (no sharding).')
    args, unknown_args = parser.parse_known_args()

    # Create a set to store arguments and create the passon string
    tests = set(arg for arg in unknown_args if arg[:2] != "--")
    passon_args = [arg for arg in unknown_args if arg[:2] == "--"]

    # Read config generated by configure.
    config = configparser.ConfigParser()
    config.read_file(open(os.path.dirname(__file__) + "/tests_config.ini"))

    enable_wallet = config["components"].getboolean("ENABLE_WALLET")
    enable_utils = config["components"].getboolean("ENABLE_UTILS")
    enable_bitcoind = config["components"].getboolean("ENABLE_BITCOIND")

    if config["environment"]["EXEEXT"] == ".exe" and not args.force:
        # https://github.com/bitcoin/bitcoin/commit/d52802551752140cf41f0d9a225a43e84404d3e9
        # https://github.com/bitcoin/bitcoin/pull/5677#issuecomment-136646964
        print("Tests currently disabled on Windows by default. Use --force option to enable")
        sys.exit(0)

    if not (enable_wallet and enable_utils and enable_bitcoind):
        print("No rpc tests to run. Wallet, utils, and bitcoind must all be enabled")
        print("Rerun `configure` with -enable-wallet, -with-utils and -with-daemon and rerun make")
        sys.exit(0)

    # Build list of tests
    if tests:
        # Individual tests have been specified. Run specified tests that exist
        # in the ALL_SCRIPTS list. Accept the name with or without .py extension.
        test_list = [t for t in ALL_SCRIPTS if
                (t in tests or re.sub(".py$", "", t) in tests)]

        print("Running individually selected tests: ")
        for t in test_list:
            print("\t" + t)
    else:
        # No individual tests have been specified; run everything enabled.
        test_list = SERIAL_SCRIPTS + FLAKY_SCRIPTS + BASE_SCRIPTS

    # Remove the test cases that the user has explicitly asked to exclude.
    if args.exclude:
        for exclude_test in args.exclude.split(','):
            if exclude_test + ".py" in test_list:
                test_list.remove(exclude_test + ".py")

    if not test_list:
        print("No valid test scripts specified. Check that your test is in one "
              "of the test lists in rpc-tests.py, or run rpc-tests.py with no arguments to run all tests")
        sys.exit(0)

    if args.help:
        # Print help for rpc-tests.py, then print help of the first script and exit.
        parser.print_help()
        subprocess.check_call((config["environment"]["SRCDIR"] + '/qa/rpc-tests/' + test_list[0]).split() + ['-h'])
        sys.exit(0)


    if (args.rpcgroup == -1) != (args.machines == -1):
        print("ERROR: Please use both -m and -r options when using parallel rpc_groups.")
        sys.exit(0)
    if args.machines == 0:
        print("ERROR: -m/--machines must be greater than 0")
        sys.exit(0)
    if args.machines > 0 and (args.rpcgroup >= args.machines):
        print("ERROR: -r/--rpcgroup must be less than -m/--machines")
        sys.exit(0)
    if args.rpcgroup != -1 and args.machines != -1 and args.machines > args.rpcgroup:
        # Ceiling division using floor division, by inverting the world.
        # https://stackoverflow.com/a/17511341
        k = -(len(test_list) // -args.machines)
        split_list = list(test_list[i*k:(i+1)*k] for i in range(args.machines))
        tests_to_run = split_list[args.rpcgroup]
    else:
        tests_to_run = test_list
    all_passed = run_tests(
        RPCTestHandler,
        tests_to_run,
        config["environment"]["SRCDIR"],
        config["environment"]["BUILDDIR"],
        config["environment"]["EXEEXT"],
        args.jobs,
        args.coverage,
        args.deterministic,
        passon_args)
    sys.exit(not all_passed)

_CACHE_BEHAVIOR_RE = re.compile(r"^\s*self\.cache_behavior\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


def _test_uses_cache(script_path):
    """True if the test touches `qa/cache`. Both 'current' (read) and
    'fresh' (force-rebuild then read) consume the shared cachedir.
    Conservative: returns True on read/parse failures."""
    try:
        with open(script_path, "r", encoding="utf8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return True
    match = _CACHE_BEHAVIOR_RE.search(source)
    if match is None:
        return True
    return match.group(1) in ('current', 'fresh')


def run_tests(test_handler, test_list, src_dir, build_dir, exeext, jobs=1, enable_coverage=False, deterministic=False, args=[]):
    BOLD = ("","")
    if os.name == 'posix':
        # primitive formatting on supported
        # terminal via ANSI escape sequences:
        BOLD = ('\033[0m', '\033[1m')

    #Set env vars
    if "ZEBRAD" not in os.environ:
        os.environ["ZEBRAD"] = os.path.join(build_dir, "src", "zebrad" + exeext)
    if "ZAINOD" not in os.environ:
        os.environ["ZAINOD"] = os.path.join(build_dir, "src", "zainod" + exeext)
    if "ZALLET" not in os.environ:
        os.environ["ZALLET"] = os.path.join(build_dir, "src", "zallet" + exeext)

    tests_dir = src_dir + '/qa/rpc-tests/'

    flags = ["--srcdir={}/src".format(build_dir)] + args
    flags.append("--cachedir=%s/qa/cache" % build_dir)

    if enable_coverage:
        coverage = RPCCoverage()
        flags.append(coverage.flag)
        print("Initializing coverage directory at %s\n" % coverage.dir)
    else:
        coverage = None

    if len(test_list) > 1 and jobs > 1:
        # Only run create_cache.py if some test will actually read it. Its
        # 8-node mesh is slow (~minutes) and flaky (zebra #10329, #10332).
        if any(_test_uses_cache(tests_dir + t.split()[0]) for t in test_list):
            subprocess.check_output([tests_dir + 'create_cache.py'] + flags)

    #Run Tests
    time_sum = 0
    time0 = time.time()

    job_queue = test_handler(jobs, tests_dir, test_list, flags)

    max_len_name = len(max(test_list, key=len))
    total_count = 0
    passed_count = 0
    results = []
    try:
        for _ in range(len(test_list)):
            (name, stdout, stderr, passed, duration) = job_queue.get_next(deterministic)
            time_sum += duration

            print('\n' + BOLD[1] + name + BOLD[0] + ":")
            print('' if passed else stdout + '\n', end='')
            print('' if stderr == '' else 'stderr:\n' + stderr + '\n', end='')
            print("Pass: %s%s%s" % (BOLD[1], passed, BOLD[0]), end='')
            if deterministic:
                print("\n", end='')
            else:
                print(", Duration: %s s" % (duration,))
            total_count += 1
            if passed:
                passed_count += 1

            new_result = "%s | %s" % (name.ljust(max_len_name), str(passed).ljust(6))
            if not deterministic:
                new_result += (" | %s s" % (duration,))
            results.append(new_result)
    except (InterruptedError, KeyboardInterrupt):
        print('\nThe following tests were running when interrupted:')
        for j in job_queue.jobs:
            print("•", j[0])
        print('\n', end='')

    all_passed = passed_count == total_count

    if all_passed:
        success_rate = "True"
    else:
        success_rate = "%d/%d" % (passed_count, total_count)
    header = "%s | PASSED" % ("TEST".ljust(max_len_name),)
    footer = "%s | %s" % ("ALL".ljust(max_len_name), str(success_rate).ljust(6))
    if not deterministic:
        header += " | DURATION"
        footer += " | %s s (accumulated)\nRuntime: %s s" % (time_sum, int(time.time() - time0))
    print(
        BOLD[1] + header + BOLD[0] + "\n\n"
        + "\n".join(sorted(results)) + "\n"
        + BOLD[1] + footer + BOLD[0])

    if coverage:
        coverage.report_rpc_coverage()

        print("Cleaning up coverage data")
        coverage.cleanup()

    return all_passed

class RPCTestHandler:
    """
    Trigger the testscrips passed in via the list.
    """

    def __init__(self, num_tests_parallel, tests_dir, test_list=None, flags=None):
        assert(num_tests_parallel >= 1)
        self.num_jobs = num_tests_parallel
        self.tests_dir = tests_dir
        self.test_list = test_list
        self.flags = flags
        self.num_running = 0
        # In case there is a graveyard of zombie bitcoinds, we can apply a
        # pseudorandom offset to hopefully jump over them.
        # (625 is PORT_RANGE/MAX_NODES)
        self.portseed_offset = int(time.time() * 1000) % 625
        self.jobs = []

    def start_test(self, args, stdout, stderr):
        return subprocess.Popen(
            args,
            universal_newlines=True,
            stdout=stdout,
            stderr=stderr)

    def get_next(self, deterministic):
        while self.num_running < self.num_jobs and self.test_list:
            # Add tests
            self.num_running += 1
            t = self.test_list.pop(0)
            port_seed = ["--portseed={}".format(len(self.test_list) + self.portseed_offset)]
            log_stdout = tempfile.SpooledTemporaryFile(max_size=2**16)
            log_stderr = tempfile.SpooledTemporaryFile(max_size=2**16)
            self.jobs.append((t,
                              time.time(),
                              self.start_test((self.tests_dir + t).split() + self.flags + port_seed,
                                               log_stdout,
                                               log_stderr),
                              log_stdout,
                              log_stderr))
            # Run serial scripts on their own. We always run these first,
            # so we won't have added any other jobs yet.
            if t in SERIAL_SCRIPTS:
                break
        if not self.jobs:
            raise IndexError('pop from empty list')
        while True:
            # Return first proc that finishes
            time.sleep(.5)
            for j in self.jobs:
                (name, time0, proc, log_out, log_err) = j
                if proc.poll() is not None:
                    log_out.seek(0), log_err.seek(0)
                    [stdout, stderr] = [l.read().decode('utf-8') for l in (log_out, log_err)]
                    log_out.close(), log_err.close()
                    # Zebra uses stderr for welcome messages and other non-error
                    # output, so we cannot use stderr emptiness as a success indicator.
                    # See https://github.com/ZcashFoundation/zebra/issues/10316
                    passed = proc.returncode == 0
                    self.num_running -= 1
                    self.jobs.remove(j)
                    return name, stdout, stderr, passed, int(time.time() - time0)
            if not deterministic:
                print('.', end='', flush=True)


class RPCCoverage(object):
    """
    Coverage reporting utilities for pull-tester.

    Coverage calculation works by having each test script subprocess write
    coverage files into a particular directory. These files contain the RPC
    commands invoked during testing, as well as a complete listing of RPC
    commands per `bitcoin-cli help` (`rpc_interface.txt`).

    After all tests complete, the commands run are combined and diff'd against
    the complete list to calculate uncovered RPC commands.

    See also: qa/rpc-tests/test_framework/coverage.py

    """
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="coverage")
        self.flag = '--coveragedir=%s' % self.dir

    def report_rpc_coverage(self):
        """
        Print out RPC commands that were unexercised by tests.

        """
        uncovered = self._get_uncovered_rpc_commands()

        if uncovered:
            print("Uncovered RPC commands:")
            print("".join(("  - %s\n" % i) for i in sorted(uncovered)))
        else:
            print("All RPC commands covered.")

    def cleanup(self):
        return shutil.rmtree(self.dir)

    def _get_uncovered_rpc_commands(self):
        """
        Return a set of currently untested RPC commands.

        """
        # This is shared from `qa/rpc-tests/test-framework/coverage.py`
        reference_filename = 'rpc_interface.txt'
        coverage_file_prefix = 'coverage.'

        coverage_ref_filename = os.path.join(self.dir, reference_filename)
        coverage_filenames = set()
        all_cmds = set()
        covered_cmds = set()

        if not os.path.isfile(coverage_ref_filename):
            raise RuntimeError("No coverage reference found")

        with open(coverage_ref_filename, 'r', encoding='utf8') as f:
            all_cmds.update([i.strip() for i in f.readlines()])

        for root, dirs, files in os.walk(self.dir):
            for filename in files:
                if filename.startswith(coverage_file_prefix):
                    coverage_filenames.add(os.path.join(root, filename))

        for filename in coverage_filenames:
            with open(filename, 'r', encoding='utf8') as f:
                covered_cmds.update([i.strip() for i in f.readlines()])

        return all_cmds - covered_cmds


if __name__ == '__main__':
    main()
