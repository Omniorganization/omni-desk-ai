from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.providers import ALLOWED_IMAGE_SUFFIXES
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.tools.base import ToolContext, bound_content, proposal, permission_guarded

DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


def _looks_like_image(suffix: str, header: bytes) -> bool:
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


class VisionGroundingTool:
    """Vision grounding tool.

    Input: image_path + instruction.
    Output: JSON containing target descriptions and optional coordinates.
    It uses ModelRouter task="vision"; no image base64 is placed into normal agent state.
    image_path must be an image file inside the workspace: the image is sent to
    an external model, so host files outside it are never readable here.
    """

    name = "vision"

    def __init__(self, router: ModelRouter, workspace_root: Path, *, max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES):
        self.router = router
        self.root = Path(workspace_root).expanduser().resolve()
        self.max_image_bytes = max_image_bytes


    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec
        return ToolSpec(
            name=self.name,
            description="Vision grounding tool for screenshots and UI elements.",
            permissions=["vision.ground"],
            actions={
                "ground": ActionSpec("ground", "Locate UI elements in screenshot", {"image_path": "string", "instruction": "string"}, risk="medium", side_effect=False, requires_approval=True)
            },
        )


    def _workspace_image(self, raw: str) -> Path:
        if not raw:
            raise ValueError("vision.ground requires image_path")
        if "\0" in raw:
            raise PermissionError("image_path must not contain NUL")
        try:
            # Relative paths stay relative to the process directory, as the
            # computer tool reports them; the containment check below decides.
            # resolve() follows every symlink, so a link that leaves the
            # workspace fails it too.
            resolved = Path(raw).expanduser().resolve()
        except RuntimeError as exc:
            raise PermissionError(f"image_path escapes workspace: {raw}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"image_path escapes workspace: {raw}") from exc
        if resolved.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise PermissionError(f"image_path type is not allowed: {resolved.name}")
        return resolved

    def _check_image_file(self, path: Path) -> str:
        """Validate the image file and return the sha256 of its content."""
        if not stat.S_ISREG(path.stat().st_mode):
            raise PermissionError(f"image_path is not a regular file: {path.name}")
        # Re-check on the opened file: no symlink swapped in, still a regular
        # file, bounded size, and the content matches the claimed type.
        fd = os.open(path, _OPEN_FLAGS)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError(f"image_path is not a regular file: {path.name}")
            if info.st_size <= 0 or info.st_size > self.max_image_bytes:
                raise PermissionError(f"image_path size {info.st_size} is outside 1..{self.max_image_bytes} bytes: {path.name}")
            header = handle.read(12)
            digest = hashlib.sha256(header)
            rest = handle.read(self.max_image_bytes + 1 - len(header))
            if len(header) + len(rest) > self.max_image_bytes:
                raise PermissionError(f"image_path grew past {self.max_image_bytes} bytes: {path.name}")
            digest.update(rest)
        if not _looks_like_image(path.suffix.lower(), header):
            raise PermissionError(f"image_path content does not match its image type: {path.name}")
        return digest.hexdigest()

    @permission_guarded
    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "ground":
            raise ValueError(f"Unsupported vision action: {action}")
        raw_path = str(args.get("image_path") or "")
        image_path = self._workspace_image(raw_path)
        instruction = str(args.get("instruction") or args.get("expected_result") or "Analyze UI screenshot")
        if not image_path.exists():
            return ToolResult(False, error=f"image_path does not exist: {image_path}")
        image_sha256 = self._check_image_file(image_path)

        # The image and the whole instruction go to the model provider: the
        # approval binds both.
        approved: dict[str, Any] = {"image_path": str(image_path), "image_sha256": image_sha256, **bound_content("instruction", instruction)}
        ctx.permissions.verify(proposal("vision", "ground", approved, "medium", "使用视觉模型分析截图并定位 UI 元素", ctx))
        # The approval can take a while: the path and file must still be what
        # was approved.
        if self._workspace_image(raw_path) != image_path:
            raise PermissionError(f"image_path changed while awaiting approval: {raw_path}")
        if self._check_image_file(image_path) != image_sha256:
            raise PermissionError(f"image_path content changed while awaiting approval: {raw_path}")

        system = (
            "You are a UI grounding model. Return strict JSON with keys: "
            "summary, elements, target, confidence. If coordinates are available, use x,y,width,height."
        )
        user = f"Analyze this screenshot for the requested UI task:\n{instruction}\nReturn JSON only."
        resp = await self.router.complete(ModelRequest(
            system=system,
            user=user,
            task="vision",
            images=[str(image_path)],
            json_mode=True,
            verified_required=True,
            task_id=str(args.get("task_id", "vision-ground")),
        ))
        text = resp.text.strip()
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"summary": text, "elements": [], "target": None, "confidence": 0}
        return ToolResult(True, data={"grounding": parsed, "provider": resp.provider, "model": resp.model}, summary="vision grounding completed")
