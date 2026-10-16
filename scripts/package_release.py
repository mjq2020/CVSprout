"""Build installers without datasets, weights, credentials, or local state."""
import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-prefix", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]+", args.image_prefix) or not re.fullmatch(r"v[0-9][A-Za-z0-9_.-]*", args.version):
        parser.error("Use a lowercase registry/repository prefix and a v-prefixed release version")
    args.output.mkdir(parents=True, exist_ok=True)
    name = f"cvsprout-{args.version}"
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / name
        staging.mkdir()
        for filename in ("start.sh", "start.ps1", "compose.yaml", "compose.gpu.yaml", "README.md", "CONTRIBUTING.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(ROOT / filename, staging / filename)
        shutil.copytree(ROOT / "docs", staging / "docs")
        environment = (ROOT / ".env.example").read_text().replace("MDP_IMAGE_PREFIX=cvsprout", f"MDP_IMAGE_PREFIX={args.image_prefix}").replace("MDP_VERSION=local", f"MDP_VERSION={args.version}")
        (staging / ".env.example").write_text(environment)
        with tarfile.open(args.output / f"{name}.tar.gz", "w:gz") as archive:
            archive.add(staging, arcname=name)
        with zipfile.ZipFile(args.output / f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in staging.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(staging.parent))
    artifacts = sorted(args.output.glob(f"{name}.*"))
    (args.output / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in artifacts))
    print(f"Created {len(artifacts)} installers in {args.output}")


if __name__ == "__main__":
    main()
