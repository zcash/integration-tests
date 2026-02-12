#!/usr/bin/env python3
#
# Execute all of the automated tests related to Zcash.
#

import argparse
from glob import glob
import os
import re
import subprocess
import sys

REPOROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__)
        )
    )
)

def repofile(filename):
    return os.path.join(REPOROOT, filename)

def get_arch_dir():
    depends_dir = os.path.join(REPOROOT, 'depends')

    arch_dir = os.path.join(depends_dir, 'x86_64-pc-linux-gnu')
    if os.path.isdir(arch_dir):
        return arch_dir

    # Not Linux, try MacOS
    arch_dirs = glob(os.path.join(depends_dir, 'x86_64-apple-darwin*'))
    if arch_dirs:
        # Just try the first one; there will only be one in CI
        return arch_dirs[0]

    # Not MacOS, try Windows
    arch_dirs = glob(os.path.join(depends_dir, 'x86_64-w64-mingw32*'))
    if arch_dirs:
        # Just try the first one; there will only be one in CI
        return arch_dirs[0]

    print("!!! cannot find architecture dir under depends/ !!!")
    return None


#
# Custom test runners
#

RE_RPATH_RUNPATH = re.compile('No RPATH.*No RUNPATH')
RE_FORTIFY_AVAILABLE = re.compile('FORTIFY_SOURCE support available.*Yes')
RE_FORTIFY_USED = re.compile('Binary compiled with FORTIFY_SOURCE support.*Yes')

CXX_BINARIES = [
]
RUST_BINARIES = [
    'src/zebrad',
    'src/zainod',
    'src/zallet',
]

def test_rpath_runpath(filename):
    output = subprocess.check_output(
        [repofile('qa/zcash/checksec.sh'), '--file=' + repofile(filename)]
    )
    if RE_RPATH_RUNPATH.search(output.decode('utf-8')):
        print('PASS: %s has no RPATH or RUNPATH.' % filename)
        return True
    else:
        print('FAIL: %s has an RPATH or a RUNPATH.' % filename)
        print(output)
        return False

def test_fortify_source(filename):
    proc = subprocess.Popen(
        [repofile('qa/zcash/checksec.sh'), '--fortify-file=' + repofile(filename)],
        stdout=subprocess.PIPE,
    )
    line1 = proc.stdout.readline()
    line2 = proc.stdout.readline()
    proc.terminate()
    if RE_FORTIFY_AVAILABLE.search(line1.decode('utf-8')) and RE_FORTIFY_USED.search(line2.decode('utf-8')):
        print('PASS: %s has FORTIFY_SOURCE.' % filename)
        return True
    else:
        print('FAIL: %s is missing FORTIFY_SOURCE.' % filename)
        return False

def check_security_hardening():
    ret = True

    # PIE, RELRO, Canary, and NX are tested by `contrib/devtools/security-check.py`.
    bin_programs = ['src/zebrad', 'src/zainod', 'src/zallet']
    bin_scripts = []

    print(f"Checking binary security of {bin_programs + bin_scripts}...")

    for program in bin_programs:
        command = [repofile('contrib/devtools/security-check.py'), repofile(program)]
        ret &= subprocess.call(command) == 0

    for script in bin_scripts:
        command = [repofile('contrib/devtools/security-check.py'), '--allow-no-canary', repofile(script)]
        ret &= subprocess.call(command) == 0

    # The remaining checks are only for ELF binaries
    # Assume that if zebrad is an ELF binary, they all are
    with open(repofile('src/zebrad'), 'rb') as f:
        magic = f.read(4)
        if not magic.startswith(b'\x7fELF'):
            return ret

    for bin in CXX_BINARIES + RUST_BINARIES:
        ret &= test_rpath_runpath(bin)

    # NOTE: checksec.sh does not reliably determine whether FORTIFY_SOURCE
    # is enabled for the entire binary. See https://github.com/zcash/zcash/issues/915 .
    # FORTIFY_SOURCE is not applicable to Rust binaries.
    for bin in CXX_BINARIES:
        ret &= test_fortify_source(bin)

    return ret

#
# Tests
#

STAGES = [
    'sec-hard',
    'rpc',
]

STAGE_COMMANDS = {
    'sec-hard': check_security_hardening,
    'rpc': [repofile('qa/pull-tester/rpc-tests.py')],
}


#
# Test driver
#

def run_stage(stage):
    print('Running stage %s' % stage)
    print('=' * (len(stage) + 14))
    print()

    cmd = STAGE_COMMANDS[stage]
    if type(cmd) == type([]):
        ret = subprocess.call(cmd) == 0
    else:
        ret = cmd()

    print()
    print('-' * (len(stage) + 15))
    print('Finished stage %s' % stage)
    print()

    return ret

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--list-stages', dest='list', action='store_true')
    parser.add_argument('stage', nargs='*', default=STAGES,
                        help='One of %s'%STAGES)
    args = parser.parse_args()

    # Check for list
    if args.list:
        for s in STAGES:
            print(s)
        sys.exit(0)

    # Check validity of stages
    for s in args.stage:
        if s not in STAGES:
            print("Invalid stage '%s' (choose from %s)" % (s, STAGES))
            sys.exit(1)

    # Run the stages
    all_passed = True
    for s in args.stage:
        passed = run_stage(s)
        if not passed:
            print("!!! Stage %s failed !!!" % (s,))
        all_passed &= passed

    if not all_passed:
        print("!!! One or more test stages failed !!!")
        sys.exit(1)

if __name__ == '__main__':
    main()
