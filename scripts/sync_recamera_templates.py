#!/usr/bin/env python3
"""Sync reviewed application sources, excluding model binaries and local state."""
import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent / "deploy/recamera/templates"
    for app_id in ("cvsprout-capture", "cvsprout-inference"):
        source = args.sdk_root / "apps" / app_id
        if not (source / "manifest.json").is_file() or not (source / "app.py").is_file():
            raise SystemExit(f"Missing app source: {source}")
        # Only reviewed source/resources, never recurse through an appdata or
        # model directory left by a developer's local run.
        files = [p for p in source.iterdir() if p.is_file() and not p.is_symlink()
                 and (p.suffix in {".py", ".json", ".txt", ".md", ".png"} or p.name == "LICENSE")
                 and p.name not in {"config.json", "release.lock.json"}]
        destination = root / app_id
        destination.mkdir(parents=True, exist_ok=True)
        for old in destination.iterdir():
            if old.is_file() and old.name not in {p.name for p in files}:
                old.unlink()
        for path in files:
            shutil.copyfile(path, destination / path.name)
        print(f"{app_id}: synchronized {len(files)} source files")


if __name__ == "__main__":
    main()
