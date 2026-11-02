"""Run local and PostgreSQL tests in isolated Python processes.

The PostgreSQL module deliberately refuses to run after app, models, or config
has been imported. Spawning each suite separately preserves that safety check
while providing one command and one combined exit status.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMMAND = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "--ignore=tests/test_postgres_integration.py",
]
POSTGRES_COMMAND = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "tests/test_postgres_integration.py",
]


def run_suite(label: str, command: list[str], environment: dict[str, str]) -> int:
    print(f"\n=== {label} ===", flush=True)
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    ).returncode


def main() -> int:
    test_url_is_set = bool(os.getenv("TEST_DATABASE_URL", "").strip())
    postgres_is_enabled = os.getenv("RUN_POSTGRES_INTEGRATION") == "1"
    if not test_url_is_set or not postgres_is_enabled:
        print(
            "Combined testing requires TEST_DATABASE_URL and "
            "RUN_POSTGRES_INTEGRATION=1. No credential was displayed.",
            file=sys.stderr,
        )
        return 2

    # The local subprocess must never collect the opt-in PostgreSQL module.
    local_environment = os.environ.copy()
    local_environment.pop("TEST_DATABASE_URL", None)
    local_environment.pop("RUN_POSTGRES_INTEGRATION", None)
    local_result = run_suite("Local test suite", LOCAL_COMMAND, local_environment)
    if local_result:
        print("\nCombined result: FAILED (local suite)", file=sys.stderr)
        return local_result

    # The integration subprocess starts with a clean module registry. The test
    # module performs all database-name, backend, and working-target safeguards.
    postgres_result = run_suite(
        "PostgreSQL integration suite",
        POSTGRES_COMMAND,
        os.environ.copy(),
    )
    if postgres_result:
        print("\nCombined result: FAILED (PostgreSQL suite)", file=sys.stderr)
        return postgres_result

    print("\nCombined result: PASSED (34 local + 5 PostgreSQL tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
