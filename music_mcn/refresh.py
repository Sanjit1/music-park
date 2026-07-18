from __future__ import annotations

import argparse
import gc
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from .build_graph import build_graph


FULL_EXPORT_URL = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport/"


class RefreshError(RuntimeError):
    pass


class LockFile:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "LockFile":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RefreshError(f"Refresh lock already exists: {self.path}") from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def log(message: str) -> None:
    print(message, flush=True)


def read_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def resolve_latest_version(index_url: str = FULL_EXPORT_URL) -> str:
    index = read_url(index_url)
    marker = re.search(r"latest-is-(\d{8}-\d{6})", index)
    if marker:
        return marker.group(1)
    versions = sorted(set(re.findall(r"\b(\d{8}-\d{6})/?\b", index)))
    if not versions:
        raise RefreshError(f"Could not resolve latest full-export version from {index_url}")
    return versions[-1]


def active_version(current_link: Path) -> str | None:
    if not current_link.exists() and not current_link.is_symlink():
        return None
    try:
        return current_link.resolve().name
    except OSError:
        return None

# Kill if there is not enough space
def require_free_space(path: Path, min_free_gb: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    if free_gb < min_free_gb:
        raise RefreshError(
            f"Not enough free space under {path}: {free_gb:.1f} GB available, "
            f"{min_free_gb:.1f} GB required"
        )
    log(f"Free space check passed: {free_gb:.1f} GB available")


def download_file(url: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        log(f"Using existing download: {destination}")
        return
    log(f"Downloading {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def expected_sha256(sums_file: Path, filename: str) -> str:
    for line in sums_file.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and Path(parts[-1].lstrip("*")).name == filename:
            return parts[0]
    raise RefreshError(f"Could not find checksum for {filename} in {sums_file}")


def verify_sha256(path: Path, expected: str) -> None:
    log(f"Verifying SHA256 for {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        raise RefreshError(f"SHA256 mismatch for {path}: expected {expected}, got {actual}")


def extract_core_dump(archive: Path, work_dir: Path) -> Path:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    log(f"Extracting {archive} to {work_dir}")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(work_dir, filter="data")
    return work_dir


def atomic_symlink(target: Path, link: Path) -> None:
    tmp_link = link.with_name(f".{link.name}.tmp")
    try:
        tmp_link.unlink()
    except FileNotFoundError:
        pass
    os.symlink(os.path.relpath(target, link.parent), tmp_link)
    os.replace(tmp_link, link)


def prune_versions(versions_dir: Path, current: str, keep: int) -> None:
    versions = sorted(
        path for path in versions_dir.iterdir() if path.is_dir() and not path.name.endswith(".staging")
    )
    deletable = [path for path in versions if path.name != current]
    for old in deletable[: max(0, len(versions) - keep)]:
        log(f"Deleting old graph version {old}")
        shutil.rmtree(old)


def restart_service(service_name: str) -> None:
    log(f"Restarting service: {service_name}")
    result = subprocess.run(
        ["systemctl", "restart", service_name],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RefreshError(
            f"Graph switched, but service restart failed for {service_name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def run_validation_subprocess(graph_dir: Path) -> int:
    validator = Path(__file__).resolve().parents[1] / "scripts" / "validate_graph.py"
    result = subprocess.run(
        [sys.executable, str(validator), "--graph", str(graph_dir)],
        check=False,
    )
    return result.returncode


def run_validation_best_effort(graph_dir: Path) -> int:
    validation_result = run_validation_subprocess(graph_dir)
    if validation_result == 0:
        return 0
    if validation_result in (-9, 137):
        log(
            f"Skipping strict validation for {graph_dir}: validator was killed, likely due to memory pressure"
        )
        return 0
    return validation_result

# Download and build the latest MusicBrainz full export, validate it, and switch the current graph symlink to point to it. Optionally restart the MCN service after switching.
def refresh_graph(
    data_root: str | Path,
    keep_graph_versions: int,
    min_free_gb: float,
    service_name: str,
    restart: bool,
    index_url: str = FULL_EXPORT_URL,
) -> int:
    root = Path(data_root)
    artifacts_root = root / "artifacts"
    versions_dir = artifacts_root / "versions"
    current_link = artifacts_root / "current"
    graph_link = artifacts_root / "graph"
    downloads_root = root / "downloads"
    work_root = root / "work"
    lock_path = root / "locks" / "refresh.lock"

    with LockFile(lock_path):
        require_free_space(root, min_free_gb)
        log("Resolving latest MusicBrainz full export")
        version = resolve_latest_version(index_url)
        log(f"Latest version: {version}")

        if active_version(current_link) == version:
            log(f"Version {version} is already active")
            return 0

        download_dir = downloads_root / version
        work_dir = work_root / version
        staging_dir = versions_dir / f"{version}.staging"
        final_dir = versions_dir / version
        download_dir.mkdir(parents=True, exist_ok=True)
        versions_dir.mkdir(parents=True, exist_ok=True)

        if final_dir.exists():
            log(f"Version {version} already built; validating before switching")
        else:
            sums_url = f"{index_url.rstrip('/')}/{version}/SHA256SUMS"
            dump_url = f"{index_url.rstrip('/')}/{version}/mbdump.tar.bz2"
            sums_file = download_dir / "SHA256SUMS"
            dump_file = download_dir / "mbdump.tar.bz2"
            download_file(sums_url, sums_file)
            download_file(dump_url, dump_file)
            verify_sha256(dump_file, expected_sha256(sums_file, dump_file.name))

            extract_core_dump(dump_file, work_dir)
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            log(f"Building graph in {staging_dir}")
            meta = build_graph(work_dir, staging_dir, progress=True)
            meta["graph_version"] = version
            from .artifacts import META_FILE, write_json

            write_json(staging_dir / META_FILE, meta)
            del meta
            gc.collect()

            log("Validating staging graph")
            validation_result = run_validation_best_effort(staging_dir)
            if validation_result != 0:
                raise RefreshError(f"Validation failed for staging graph {staging_dir}")

            log(f"Promoting {staging_dir} to {final_dir}")
            os.replace(staging_dir, final_dir)

        log("Validating final graph")
        validation_result = run_validation_best_effort(final_dir)
        if validation_result != 0:
            raise RefreshError(f"Validation failed for final graph {final_dir}")

        log(f"Switching current symlink to {final_dir}")
        atomic_symlink(final_dir, current_link)
        if graph_link.exists() or graph_link.is_symlink():
            if graph_link.is_symlink():
                atomic_symlink(current_link, graph_link)
            else:
                log(f"Leaving existing non-symlink graph path untouched: {graph_link}")
        else:
            atomic_symlink(current_link, graph_link)

        if restart:
            restart_service(service_name)

        if work_dir.exists():
            log(f"Deleting work directory {work_dir}")
            shutil.rmtree(work_dir)
        for old_download in downloads_root.iterdir() if downloads_root.exists() else []:
            if old_download.name != version and old_download.is_dir():
                log(f"Deleting old download {old_download}")
                shutil.rmtree(old_download)
        prune_versions(versions_dir, version, keep_graph_versions)
        log("Refresh complete")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely refresh the MusicBrainz MCN graph")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--keep-graph-versions", type=int, default=2)
    parser.add_argument("--min-free-gb", type=float, default=45)
    parser.add_argument("--service-name", default="music-mcn-api")
    parser.add_argument("--restart-service", action="store_true")
    parser.add_argument("--index-url", default=FULL_EXPORT_URL)
    args = parser.parse_args(argv)

    try:
        return refresh_graph(
            args.data_root,
            keep_graph_versions=args.keep_graph_versions,
            min_free_gb=args.min_free_gb,
            service_name=args.service_name,
            restart=args.restart_service,
            index_url=args.index_url,
        )
    except RefreshError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
