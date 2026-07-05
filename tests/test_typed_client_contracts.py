from __future__ import annotations

import json
from pathlib import Path

from scripts import check_typed_client_contracts as checker


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_contract(root: Path, endpoints: list[dict[str, object]]) -> None:
    _write(
        root,
        "apps/shared/omni-app-api.contract.json",
        json.dumps({"endpoints": endpoints}),
    )


def test_typed_client_contract_coverage_is_method_specific(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        checker,
        "REQUIRED_SURFACE_ROUTES",
        {"web_admin": (("GET", "/app/conversations"), ("POST", "/app/conversations"))},
    )
    monkeypatch.setattr(
        checker,
        "TYPED_TEST_FILES",
        {"web_admin": {"path": "apps/web-admin-next/tests/api.test.ts", "marker": "WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES"}},
    )
    _write_contract(
        tmp_path,
        [
            {"method": "GET", "path": "/app/conversations", "role": "operator"},
            {"method": "POST", "path": "/app/conversations", "role": "operator"},
        ],
    )
    _write(
        tmp_path,
        "apps/web-admin-next/tests/api.test.ts",
        "const WEB_ADMIN_TYPED_CLIENT_CONTRACT_CASES = ["
        "{ method: 'GET', contractPath: '/app/conversations', signedInProduction: true }"
        "];",
    )

    assert checker._check_contract_coverage(tmp_path) == [
        "typed client contract: web_admin test does not cover POST /app/conversations"
    ]


def test_typed_client_contract_signed_coverage_is_endpoint_specific(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        checker,
        "REQUIRED_SURFACE_ROUTES",
        {
            "mobile": (
                ("POST", "/app/approvals/{approval_id}/decide"),
                ("POST", "/app/devices/{device_id}/push-token"),
            )
        },
    )
    monkeypatch.setattr(
        checker,
        "TYPED_TEST_FILES",
        {"mobile": {"path": "apps/mobile-flutter/test/omni_api_test.dart", "marker": "mobileTypedClientContractCases"}},
    )
    _write_contract(
        tmp_path,
        [
            {
                "method": "POST",
                "path": "/app/approvals/{approval_id}/decide",
                "role": "operator",
                "signed_device_required_in_production": ["mobile"],
            },
            {
                "method": "POST",
                "path": "/app/devices/{device_id}/push-token",
                "role": "operator",
                "signed_device_required_in_production": ["mobile"],
            },
        ],
    )
    _write(
        tmp_path,
        "apps/mobile-flutter/test/omni_api_test.dart",
        """
        const mobileTypedClientContractCases = <TypedClientContractCase>[
          TypedClientContractCase(
            method: 'POST',
            contractPath: '/app/approvals/{approval_id}/decide',
            signedInProduction: true,
          ),
          TypedClientContractCase(
            method: 'POST',
            contractPath: '/app/devices/{device_id}/push-token',
          ),
        ];
        """,
    )

    assert checker._check_contract_coverage(tmp_path) == [
        "typed client contract: mobile must assert signed production route POST /app/devices/{device_id}/push-token"
    ]
