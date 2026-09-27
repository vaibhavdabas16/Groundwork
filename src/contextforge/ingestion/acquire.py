"""Acquire the original Olist Brazilian E-Commerce Public Dataset.

Supported acquisition modes (no dataset substitution, no synthetic records):

* ``kaggle``  - download the published archive from Kaggle's public dataset endpoint.
  Public datasets can be downloaded anonymously. If ``KAGGLE_USERNAME``/``KAGGLE_KEY`` are set
  in the environment they are sent as HTTP basic auth (needed if Kaggle ever requires auth).
* ``zip``     - import a manually downloaded ``archive.zip`` (verified manual-import workflow).
* ``dir``     - import a directory that already contains the nine original CSV files.

Every mode verifies that exactly the expected original files are present, computes SHA-256
checksums, and writes ``data/manifests/olist_source_manifest.json``. If a previous manifest
exists, checksums are compared so a silently changed upstream snapshot is detected.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from contextforge.common.hashing import sha256_file
from contextforge.config.settings import Settings
from contextforge.observability.logging import get_logger

log = get_logger("ingestion")

DATASET_REF = "olistbr/brazilian-ecommerce"
DATASET_PAGE_URL = "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce"
DOWNLOAD_URL = f"https://www.kaggle.com/api/v1/datasets/download/{DATASET_REF}"
METADATA_URL = f"https://www.kaggle.com/api/v1/datasets/view/{DATASET_REF}"

# Original file name -> logical source table name.
EXPECTED_FILES: dict[str, str] = {
    "olist_customers_dataset.csv": "customers",
    "olist_geolocation_dataset.csv": "geolocation",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_orders_dataset.csv": "orders",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "product_category_name_translation.csv": "product_category_name_translation",
}


class AcquisitionError(RuntimeError):
    pass


def _source_dir(settings: Settings) -> Path:
    return settings.raw_dir / "olist"


def manifest_path(settings: Settings) -> Path:
    return settings.manifest_dir / "olist_source_manifest.json"


def _kaggle_auth() -> tuple[str, str] | None:
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    return (user, key) if user and key else None


def fetch_metadata(timeout: float = 60.0) -> dict[str, Any]:
    """Public dataset metadata (license, version, title). Returns {} if unavailable."""
    try:
        resp = httpx.get(METADATA_URL, timeout=timeout, auth=_kaggle_auth(), follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("dataset metadata unavailable", extra={"error": str(exc)})
        return {}
    return {
        "title": data.get("titleNullable"),
        "license": data.get("licenseNameNullable"),
        "creator": data.get("creatorNameNullable"),
        "current_version_number": data.get("currentVersionNumberNullable"),
        "last_updated": data.get("lastUpdated") or data.get("lastUpdatedNullable"),
        "total_bytes": data.get("totalBytesNullable"),
        "dataset_id": data.get("id") or data.get("datasetId"),
    }


def _download_archive(dest: Path, timeout: float = 600.0) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading dataset archive", extra={"url": DOWNLOAD_URL})
    with httpx.stream(
        "GET", DOWNLOAD_URL, timeout=timeout, auth=_kaggle_auth(), follow_redirects=True
    ) as resp:
        if resp.status_code in (401, 403):
            raise AcquisitionError(
                f"Kaggle refused the download (HTTP {resp.status_code}). Set KAGGLE_USERNAME and "
                "KAGGLE_KEY, or download archive.zip manually from "
                f"{DATASET_PAGE_URL} and run: contextforge acquire --zip <path>"
            )
        resp.raise_for_status()
        final_url = str(resp.url)
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    # Strip signed query parameters from the recorded URL (they are short-lived tokens).
    return final_url.split("?", 1)[0]


def _extract_expected(zip_path: Path, out_dir: Path) -> None:
    """Extract only the expected member names (guards against zip-slip and extra content)."""
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        missing = sorted(set(EXPECTED_FILES) - names)
        if missing:
            raise AcquisitionError(f"archive is missing original files: {missing}")
        unexpected = sorted(n for n in names if n not in EXPECTED_FILES)
        if unexpected:
            log.warning("archive contains unexpected members (ignored)", extra={"members": unexpected})
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in EXPECTED_FILES:
            with zf.open(name) as src, (out_dir / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return next(csv.reader(io.StringIO(fh.readline())))


def acquire(
    settings: Settings,
    *,
    mode: str = "kaggle",
    zip_path: Path | None = None,
    dir_path: Path | None = None,
) -> dict[str, Any]:
    out_dir = _source_dir(settings)
    archive_sha: str | None = None
    retrieved_from: str
    if mode == "kaggle":
        archive = settings.raw_dir / "archive.zip"
        retrieved_from = _download_archive(archive)
        archive_sha = sha256_file(archive)
        _extract_expected(archive, out_dir)
    elif mode == "zip":
        if zip_path is None or not zip_path.is_file():
            raise AcquisitionError(f"zip file not found: {zip_path}")
        archive_sha = sha256_file(zip_path)
        retrieved_from = f"manual-import:zip:{zip_path.name}"
        _extract_expected(zip_path, out_dir)
    elif mode == "dir":
        if dir_path is None or not dir_path.is_dir():
            raise AcquisitionError(f"directory not found: {dir_path}")
        missing = [n for n in EXPECTED_FILES if not (dir_path / n).is_file()]
        if missing:
            raise AcquisitionError(f"directory is missing original files: {missing}")
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in EXPECTED_FILES:
            if (dir_path / name).resolve() != (out_dir / name).resolve():
                shutil.copyfile(dir_path / name, out_dir / name)
        retrieved_from = "manual-import:dir"
    else:
        raise AcquisitionError(f"unknown acquisition mode: {mode}")

    files: list[dict[str, Any]] = []
    for name, table in EXPECTED_FILES.items():
        p = out_dir / name
        files.append(
            {
                "file": name,
                "table": table,
                "sha256": sha256_file(p),
                "bytes": p.stat().st_size,
                "header": _header(p),
            }
        )

    previous = load_manifest(settings)
    drift: list[str] = []
    if previous:
        prev = {f["file"]: f["sha256"] for f in previous.get("files", [])}
        drift = [f["file"] for f in files if f["file"] in prev and prev[f["file"]] != f["sha256"]]

    metadata = fetch_metadata()
    manifest = {
        "dataset": "Brazilian E-Commerce Public Dataset by Olist",
        "dataset_ref": DATASET_REF,
        "source_page": DATASET_PAGE_URL,
        "download_endpoint": DOWNLOAD_URL,
        "retrieved_from": retrieved_from,
        "acquisition_mode": mode,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "archive_sha256": archive_sha,
        "kaggle_metadata": metadata,
        "license": metadata.get("license") or (previous or {}).get("license"),
        "license_verified_from": METADATA_URL if metadata.get("license") else None,
        "files": files,
        "checksum_drift_vs_previous_manifest": drift,
    }
    settings.manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path(settings).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if drift:
        log.warning("source checksums differ from previous manifest", extra={"files": drift})
    log.info("acquisition complete", extra={"files": len(files), "mode": mode})
    return manifest


def load_manifest(settings: Settings) -> dict[str, Any] | None:
    p = manifest_path(settings)
    if not p.is_file():
        return None
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def verify_raw_files(settings: Settings) -> list[str]:
    """Return a list of problems comparing on-disk raw files to the recorded manifest."""
    manifest = load_manifest(settings)
    if manifest is None:
        return ["no source manifest: run `contextforge acquire` first"]
    problems = []
    for entry in manifest["files"]:
        p = _source_dir(settings) / entry["file"]
        if not p.is_file():
            problems.append(f"missing raw file {entry['file']}")
        elif sha256_file(p) != entry["sha256"]:
            problems.append(f"checksum mismatch for {entry['file']}")
    return problems


def raw_file(settings: Settings, table: str) -> Path:
    for name, t in EXPECTED_FILES.items():
        if t == table:
            return _source_dir(settings) / name
    raise KeyError(table)
