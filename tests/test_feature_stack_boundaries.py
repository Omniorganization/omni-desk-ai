from __future__ import annotations

from pathlib import Path

from scripts.check_feature_stack_boundaries import _check_typed_client_contracts


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_typed_client_boundary_accepts_explicit_case_array_annotation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "scripts/check_typed_client_contracts.py",
        "TYPED_TEST_FILES\nclient_surfaces\ncontract-declared\ntyped client contract tests verified from contract client_surfaces\n",
    )
    _write(
        tmp_path,
        "apps/shared/omni-app-api.contract.json",
        '{"client_surface_policy": "fixture", "endpoints": [{"client_surfaces": []}]}\n',
    )
    _write(
        tmp_path,
        "apps/web-admin-next/tests/api.test.ts",
        "const WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES: readonly TypedContractCase[] = [];\n",
    )
    _write(
        tmp_path,
        "apps/desktop-tauri/tests/api.test.ts",
        "const DESKTOP_TYPED_CLIENT_CONTRACT_CASES = [] as const satisfies readonly TypedContractCase[];\n",
    )
    _write(
        tmp_path,
        "apps/mobile-flutter/test/omni_api_test.dart",
        "final mobileTypedClientContractCases = <TypedClientContractCase>[];\n",
    )
    _write(
        tmp_path,
        "Makefile",
        "typed-client-contracts:\n\tpython scripts/check_typed_client_contracts.py .\n",
    )
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "Typed client contract tests\nscripts/check_typed_client_contracts.py .\n",
    )
    _write(
        tmp_path,
        ".github/workflows/tri-app-quality.yml",
        "Typed client contract coverage\nscripts/check_typed_client_contracts.py .\n",
    )

    assert _check_typed_client_contracts(tmp_path) == []
