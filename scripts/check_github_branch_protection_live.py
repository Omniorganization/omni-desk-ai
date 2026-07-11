#!/usr/bin/env python3
"""Compatibility entrypoint for the canonical live branch protection checker."""

try:
    from scripts.check_live_branch_protection_contract import LiveCheckResult, evaluate_live_protection, main
except ModuleNotFoundError:  # Direct execution sets scripts/ as sys.path[0].
    from check_live_branch_protection_contract import LiveCheckResult, evaluate_live_protection, main

__all__ = ["LiveCheckResult", "evaluate_live_protection", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
