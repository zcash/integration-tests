#!/usr/bin/env python3
"""Decide whether a set of changed files requires running CI.

Extracted from the inline ./subclass.py heredoc in the "no-ci-required"
job of .github/workflows/ci-skip.yml. Reads the list of changed file
paths from stdin (one per line) and prints either:

    result=verified   all changed files match the filtered paths
    result=skipped    at least one changed file is outside them

Exits with status 1 if no changed files are provided.
"""

import os
import re
import shlex
import sys

paths = [
    r'^\.github/dependabot\.yml$',
    r'^\.github/workflows/audits\.yml$',
    r'^\.github/workflows/book\.yml$',
    r'^\.github/workflows/ci-skip\.yml$',
    r'^\.github/workflows/lints\.yml$',
    r'^\.github/workflows/release-docker-hub\.yml$',
    r'^contrib/debian/copyright$',
    r'^doc/.*',
    r'.*\.md$',
    r'^COPYING$',
    r'^INSTALL$',
]
paths_regex = '(?:%s)' % '|'.join(paths)

lex = shlex.shlex(posix = True)
lex.whitespace = '\n\r'
lex.whitespace_split = True
lex.commenters = ''
changed_files = list(lex)
if len(changed_files) == 0:
    sys.exit(1)

verified = True
for f in changed_files:
    if not re.match(paths_regex, f):
        verified = False

print('result=verified' if verified else 'result=skipped')
