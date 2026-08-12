from __future__ import annotations

import zipfile
from pathlib import Path


STORE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".zip",
    ".gz",
    ".7z",
    ".rar",
    ".mp4",
    ".mov",
}


def zip_paths(
    pairs: list[tuple[Path, str]],
    archive_path: Path,
) -> Path:
    """Write zip with STORE for already-compressed types; DEFLATE otherwise."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = archive_path.with_suffix(archive_path.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for src, arcname in pairs:
            if not src.is_file():
                continue
            ext = src.suffix.lower()
            compress = zipfile.ZIP_STORED if ext in STORE_EXTS else zipfile.ZIP_DEFLATED
            zf.write(src, arcname, compress_type=compress)
    tmp.replace(archive_path)
    return archive_path
