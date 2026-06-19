from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assert_controls(actual_controls: tuple[str, ...], expected_controls: tuple[str, ...]) -> AssertionResult:
    missing = tuple(control for control in expected_controls if control not in actual_controls)
    return AssertionResult(passed=not missing, failures=tuple(f"missing control: {item}" for item in missing))
