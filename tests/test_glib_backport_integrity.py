import shutil
from pathlib import Path

import pytest

from scripts.check_glib_backport import check


def test_backport_has_exact_published_source_and_upstream_fix() -> None:
    result = check(Path("."))
    assert result["exact_patch_verified"]
    assert result["file_count"] == 121


@pytest.mark.parametrize("mutation", ["revert", "extra_file", "manifest", "registry"])
def test_backport_gate_rejects_unreviewed_or_bypassed_source(tmp_path: Path, mutation: str) -> None:
    native = Path("apps/desktop-tauri/src-tauri")
    copy = tmp_path / native
    copy.mkdir(parents=True)
    shutil.copytree(native / "vendor", copy / "vendor")
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copyfile(native / name, copy / name)
    if mutation == "revert":
        path = copy / "vendor/glib/src/variant_iter.rs"
        path.write_text(path.read_text().replace("&mut p,", "&p,"))
    elif mutation == "extra_file":
        (copy / "vendor/glib/src/unreviewed.rs").write_text("// unreviewed\n")
    elif mutation == "manifest":
        path = copy / "Cargo.toml"
        path.write_text(path.read_text().replace('path = "vendor/glib"', 'path = "other"'))
    else:
        path = copy / "Cargo.lock"
        path.write_text(path.read_text().replace('name = "glib"\n', 'name = "glib"\nsource = "registry"\n'))
    with pytest.raises(ValueError):
        check(tmp_path)
