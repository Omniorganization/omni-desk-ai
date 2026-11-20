"""Version negotiation for the tri-app API contract.

The contract is apps/shared/omni-app-api.contract.json. Clients send
``X-OmniDesk-Contract: MAJOR.MINOR``, and the gateway answers every contract
route (``/app``, ``/api/chat``, ``/admin``) with its own version in the same
header. A minor version only adds routes or optional fields, so a client of the
same major is served. A client of another major is refused with 426 before the
request runs, and the clients refuse a gateway of another major. A request
without the header comes from a client built before negotiation and is served.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, MutableMapping

CONTRACT_VERSION = "1.0"
CONTRACT_HEADER = "x-omnidesk-contract"

_VERSION_RE = re.compile(r"^([0-9]{1,4})\.([0-9]{1,4})$")
_CONTRACT_PREFIXES = ("/app", "/api/chat", "/admin")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def contract_major(value: str) -> int | None:
    match = _VERSION_RE.fullmatch(value.strip())
    return int(match.group(1)) if match else None


def _is_contract_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _CONTRACT_PREFIXES)


class ContractVersionMiddleware:
    """Refuses clients of another major contract version and labels responses."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], version: str = CONTRACT_VERSION) -> None:
        major = contract_major(version)
        if major is None:
            raise ValueError(f"contract version must be MAJOR.MINOR, not {version!r}")
        self.app = app
        self.version = version
        self.major = major
        self._header = (CONTRACT_HEADER.encode(), version.encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_contract_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        claimed = next((value.decode("latin-1").strip() for name, value in scope.get("headers", []) if name == self._header[0]), None)
        if claimed is not None:
            major = contract_major(claimed)
            if major != self.major:
                code = "contract_version_invalid" if major is None else "contract_version_unsupported"
                body = json.dumps({"ok": False, "code": code, "error": code, "gateway_contract": self.version, "client_contract": claimed[:32]}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 400 if major is None else 426,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), self._header],
                })
                await send({"type": "http.response.body", "body": body})
                return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), self._header]}
            await send(message)

        await self.app(scope, receive, send_with_version)
