"""Test runner: python run_tests.py [-v]. Mirrors failures to GITHUB_STEP_SUMMARY and
emits ::error:: annotations when running inside GitHub Actions."""

import os
import sys
import unittest


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 1
    root = os.path.dirname(os.path.abspath(__file__))
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(root, "tests"), top_level_dir=root)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write("## ssipng tests\n\n")
                fh.write("- ran: %d\n- failures: %d\n- errors: %d\n"
                         % (result.testsRun, len(result.failures), len(result.errors)))
                for test, tb in result.failures + result.errors:
                    fh.write("\n<details><summary>%s</summary>\n\n```\n%s\n```\n</details>\n"
                             % (test, tb))
        except OSError:
            pass

    if os.environ.get("GITHUB_ACTIONS"):
        for test, tb in result.failures + result.errors:
            first = tb.strip().splitlines()[-1][:150]
            print("::error title=%s::%s" % (test, first), flush=True)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
