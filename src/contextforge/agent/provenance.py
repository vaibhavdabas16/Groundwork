"""Provenance stamps recorded on every trace: code version, dataset snapshot, context version."""

from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from contextforge.config.settings import Settings


@lru_cache(maxsize=4)
def code_version(root: Path) -> dict[str, str | None]:
    """Git commit (if any) plus a content hash of the Python sources (works without commits)."""
    h = hashlib.sha256()
    src = root / "src" / "contextforge"
    for p in sorted(src.rglob("*.py")):
        h.update(p.relative_to(src).as_posix().encode())
        h.update(p.read_bytes())
    sha: str | None = None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - optional; absent git -> sha None
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode == 0:
            sha = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        sha = None
    return {"git_sha": sha, "source_hash": "src-" + h.hexdigest()[:16]}


@lru_cache(maxsize=4)
def dataset_snapshot(serving_path: Path, manifest_dir: Path) -> dict[str, Any]:
    """Identity of the evaluation snapshot.

    ``snapshot_id`` is content-based and deployment-independent: SHA-256 over the source-file
    checksums (data/manifests) and the warehouse build code. It deliberately excludes the
    pseudonymisation key, which changes key values but no aggregate. ``serving_db_sha256`` (the
    physical file) is recorded as well for local forensics.
    """
    h: Any = hashlib.sha256()
    if serving_path.is_file():
        with open(serving_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    else:  # warehouse unavailable: record it; the governed query path reports the outage
        h = None
    source = None
    ident = hashlib.sha256()
    mpath = manifest_dir / "olist_source_manifest.json"
    if mpath.is_file():
        m = json.loads(mpath.read_text())
        source = m.get("archive_sha256")
        for f_ in sorted(m.get("files", []), key=lambda x: x["file"]):
            ident.update(f"{f_['file']}={f_['sha256']};".encode())
    wh = Path(__file__).resolve().parents[1] / "warehouse"
    for name in ("schema.py", "build.py"):
        ident.update((wh / name).read_bytes().replace(b"\r\n", b"\n"))  # line-ending independent
    return {
        "snapshot_id": "snap-" + ident.hexdigest()[:24],
        "serving_db_sha256": h.hexdigest() if h is not None else None,
        "source_archive_sha256": source,
    }


def provenance(settings: Settings) -> dict[str, Any]:
    return {
        "code": code_version(settings.root),
        "dataset": dataset_snapshot(settings.serving_path, settings.manifest_dir),
    }
