#!/usr/bin/env python3
#
# Unit tests for the CI helper scripts in this directory. Standard library
# only, so they run without installing anything:
#
#   python3 -m unittest discover -s .github/scripts -p 'test_*.py'

import io
import unittest
from collections import Counter
from unittest import mock

import check_no_ci_required
import run_rpc_tests
import set_rpc_test_shards
from set_rpc_test_shards import SHARDS, rpc_tests


def all_assigned(shards: list[set_rpc_test_shards.Shard]) -> Counter[str]:
    return Counter(t for s in shards for t in s["rpc_tests"])


class AssignShardsTest(unittest.TestCase):
    def test_partitions_every_test_exactly_once(self) -> None:
        expected = Counter(
            rpc_tests.BASE_SCRIPTS
            + rpc_tests.ZMQ_SCRIPTS
            + rpc_tests.NEW_SCRIPTS
            + rpc_tests.SERIAL_SCRIPTS
            + rpc_tests.FLAKY_SCRIPTS
        )
        shards = set_rpc_test_shards.assign_shards(is_interop_request=False)
        self.assertEqual(all_assigned(shards), expected)

    def test_partitions_for_any_group_sizes(self) -> None:
        # The proportional split once rounded a small group down to zero
        # shards, dropping its tests; check every size combination near the
        # edges, not just the current lists.
        sizes = [0, 1, 2, SHARDS - 1, SHARDS, 30, 100]
        for n_old in sizes:
            for n_new in sizes:
                old = [f"old_{i}.py" for i in range(n_old)]
                new = [f"new_{i}.py" for i in range(n_new)]
                with (
                    self.subTest(n_old=n_old, n_new=n_new),
                    mock.patch.multiple(
                        rpc_tests,
                        BASE_SCRIPTS=old,
                        ZMQ_SCRIPTS=[],
                        NEW_SCRIPTS=new,
                        SERIAL_SCRIPTS=[],
                        FLAKY_SCRIPTS=[],
                    ),
                ):
                    shards = set_rpc_test_shards.assign_shards(False)
                    self.assertEqual(all_assigned(shards), Counter(old + new))
                    self.assertLessEqual(len(shards), SHARDS)
                    self.assertTrue(all(s["rpc_tests"] for s in shards))

    def test_interop_request_runs_only_new_tests(self) -> None:
        shards = set_rpc_test_shards.assign_shards(is_interop_request=True)
        self.assertEqual(all_assigned(shards), Counter(rpc_tests.NEW_SCRIPTS))

    def test_shards_are_named_uniquely_and_non_empty(self) -> None:
        for interop in (False, True):
            with self.subTest(interop=interop):
                shards = set_rpc_test_shards.assign_shards(interop)
                names = [s["shard"] for s in shards]
                self.assertEqual(len(names), len(set(names)))
                self.assertTrue(all(s["rpc_tests"] for s in shards))
                parallel = [n for n in names if n.startswith("shard-")]
                self.assertLessEqual(len(parallel), SHARDS)


class NoCiRequiredTest(unittest.TestCase):
    def check(self, diff: str) -> bool:
        files = check_no_ci_required.read_changed_files(io.StringIO(diff))
        return check_no_ci_required.no_ci_required(files)

    def test_docs_only_change_skips_ci(self) -> None:
        self.assertTrue(self.check("README.md\ndoc/book/src/ci.md\nCOPYING\n"))

    def test_any_other_file_requires_ci(self) -> None:
        self.assertFalse(self.check("README.md\nqa/rpc-tests/wallet.py\n"))
        self.assertFalse(self.check(".github/workflows/ci.yml\n"))
        self.assertFalse(self.check(".github/scripts/run_rpc_tests.py\n"))

    def test_patterns_are_anchored(self) -> None:
        self.assertFalse(self.check("qa/COPYING\n"))
        self.assertFalse(self.check("src/doc/x\n"))

    def test_quoted_paths_are_unquoted(self) -> None:
        # git quotes names with unusual characters; the quotes are stripped.
        files = check_no_ci_required.read_changed_files(
            io.StringIO('"doc/a b.md"\nqa/x.py\n')
        )
        self.assertEqual(files, ["doc/a b.md", "qa/x.py"])


class ParseTestListTest(unittest.TestCase):
    def test_accepts_list_of_strings(self) -> None:
        self.assertEqual(
            run_rpc_tests.parse_test_list('["a.py", "b.py --x"]'),
            ["a.py", "b.py --x"],
        )

    def test_rejects_other_shapes(self) -> None:
        for raw in ("[]", '"a.py"', "[1]", "{}", "null"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                run_rpc_tests.parse_test_list(raw)


if __name__ == "__main__":
    unittest.main()
