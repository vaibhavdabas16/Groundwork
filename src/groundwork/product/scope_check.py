"""CI guard for AI-proposed context PRs: every changed path must be on the proposal allow-list and
no added line may introduce certification. Usage (in CI, trusted code, PR checked out as data):

    python -m groundwork.product.scope_check --base origin/main

Exit 0 = in scope, 1 = violation (prints each offending path/line). Runs on every PR from a
``groundwork/proposal-*`` branch or labelled ``ai-proposed``; a human PR that touches code is
unaffected because the job only gates proposal PRs.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from groundwork.product.proposals.patches import PatchError, check_repo_path
from groundwork.product.proposals.validation import FORBIDDEN_ADDITIONS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    args = ap.parse_args(argv)
    git = shutil.which("git") or "git"
    names = subprocess.run(  # noqa: S603 - fixed argv
        [git, "diff", "--name-only", f"{args.base}...HEAD"], check=True, capture_output=True, text=True
    ).stdout.split()
    diff = subprocess.run(  # noqa: S603 - fixed argv
        [git, "diff", "-U0", f"{args.base}...HEAD"], check=True, capture_output=True, text=True
    ).stdout
    problems: list[str] = []
    for n in names:
        try:
            check_repo_path(n)
        except PatchError as exc:
            problems.append(str(exc))
    for line in diff.splitlines():
        if (
            line.startswith("+")
            and not line.startswith("+++")
            and any(f in line for f in FORBIDDEN_ADDITIONS)
        ):
            problems.append(f"adds certification: {line[:160]}")
    for p in problems:
        sys.stdout.write(f"::error::{p}\n")
    sys.stdout.write(f"{len(names)} changed file(s); {len(problems)} problem(s)\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
