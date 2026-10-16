#!/usr/bin/env python3
"""Ensure the packaged Hermes Desktop matches its pinned Electron runtime.

The updater calls this after source integrations are restored.  It is idempotent:
when package metadata, node_modules and the packaged release agree, it exits
without rebuilding.  A rebuild is staged outside the live release and is only
swapped in after version verification.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _version(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    proc = subprocess.run(command, cwd=cwd, env=env, check=False)
    if proc.returncode:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(command)}")


def ensure(repo: Path, *, verify_only: bool = False) -> tuple[int, str]:
    repo = repo.resolve()
    desktop = repo / "apps" / "desktop"
    package = json.loads((desktop / "package.json").read_text(encoding="utf-8"))
    expected = str(package.get("build", {}).get("electronVersion") or "")
    dependency = str(package.get("devDependencies", {}).get("electron") or "")
    if not expected or dependency != expected:
        return 1, "desktop Electron pin/build version mismatch"

    node_version = _version(repo / "node_modules" / "electron" / "dist" / "version")
    release = desktop / "release" / "win-unpacked"
    release_version = _version(release / "version")
    # UI freshness gate: the Electron pin alone cannot prove the packaged
    # desktop is current. Rebuilt dist newer than the packaged asar means the
    # client bundle is stale even when the Electron version still matches.
    # Treat that as stale so the update flow rebuilds the desktop instead of
    # skipping it (upstream issue #88827 pattern).
    dist_index = desktop / "dist" / "index.html"
    asar_path = release / "resources" / "app.asar"
    ui_stale = (
        dist_index.exists()
        and asar_path.exists()
        and dist_index.stat().st_mtime > asar_path.stat().st_mtime
    )
    if node_version == expected and release_version == expected and (release / "Hermes.exe").is_file() and not ui_stale:
        return 0, f"desktop runtime present: Electron {expected}"
    if verify_only:
        return 1, f"desktop runtime stale: expected={expected}, node={node_version or 'missing'}, release={release_version or 'missing'}, ui_stale={ui_stale}"

    env = dict(os.environ)
    env.setdefault("ELECTRON_GET_USE_PROXY", "1")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return 1, "npm not found"
    try:
        _run([npm, "install", "--workspace", "apps/desktop"], repo, env)
        node_version = _version(repo / "node_modules" / "electron" / "dist" / "version")
        if node_version != expected:
            return 1, f"Electron install incomplete: expected={expected}, got={node_version or 'missing'}"
        _run([npm, "run", "build", "--workspace", "apps/desktop"], repo, env)
        staging_name = "release-update-staging"
        staging = desktop / staging_name
        shutil.rmtree(staging, ignore_errors=True)
        _run(
            [npm, "run", "builder", "--workspace", "apps/desktop", "--", "--dir", f"--config.directories.output={staging_name}"],
            repo,
            env,
        )
        candidate = staging / "win-unpacked"
        candidate_version = _version(candidate / "version")
        if candidate_version != expected or not (candidate / "Hermes.exe").is_file():
            return 1, f"packaged Desktop verification failed: expected={expected}, got={candidate_version or 'missing'}"
        previous = desktop / "release-update-previous"
        shutil.rmtree(previous, ignore_errors=True)
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.exists():
            os.replace(release, previous)
        try:
            os.replace(candidate, release)
        except Exception:
            if previous.exists() and not release.exists():
                os.replace(previous, release)
            raise
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
    except Exception as exc:
        return 1, str(exc)
    return 0, f"desktop runtime rebuilt: Electron {expected}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    code, message = ensure(args.repo, verify_only=args.verify_only)
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
