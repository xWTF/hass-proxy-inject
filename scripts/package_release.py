"""Validate a release tag and produce a component-root HACS ZIP in CI."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path

DOMAIN = "proxy_inject"
COMPONENT = Path("custom_components") / DOMAIN
VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|-(?:alpha|beta|rc)\.\d+)?")
REQUIRED = {
    "manifest.json",
    "__init__.py",
    "aiohttp_inject.py",
    "httpx_inject.py",
    "config_flow.py",
    "const.py",
    "policy.py",
    "strings.json",
    "translations/en.json",
    "translations/zh-Hans.json",
    "brand/icon.png",
    "brand/README.md",
    "README.md",
}


def build_release(root: Path, tag: str, output: Path):
    """Package tracked files only, with consistent paths, timestamps and modes."""
    root = root.resolve()
    component = root / COMPONENT
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version", "")
    if (
        manifest.get("domain") != DOMAIN
        or not VERSION.fullmatch(version)
        or tag.removeprefix("v") != version
    ):
        raise ValueError("Tag must match manifest.json version (optional v prefix)")

    tracked = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--", COMPONENT.as_posix(), "README.md"], cwd=root
        )
        .decode("utf-8")
        .split("\0")
    )
    contents = {}
    for name in sorted(filter(None, tracked)):
        source = root / name
        relative = (
            Path("README.md") if name == "README.md" else source.relative_to(component)
        )
        if "__pycache__" in relative.parts or source.suffix in (".pyc", ".pyo"):
            continue
        target = source.resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"Release file must resolve inside repository: {name}")
        if relative.as_posix() in contents:
            raise ValueError(f"Duplicate release path: {relative}")
        contents[relative.as_posix()] = target.read_bytes()
    if missing := REQUIRED - contents.keys():
        raise ValueError(f"Required release files are not tracked: {sorted(missing)}")

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{DOMAIN}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(contents.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )
    return archive, re.fullmatch(r"\d+\.\d+\.\d+", version) is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    archive, prerelease = build_release(
        Path(__file__).resolve().parents[1], args.tag, args.output
    )
    if output_file := os.environ.get("GITHUB_OUTPUT"):
        with open(output_file, "a", encoding="utf-8") as output:
            output.write(f"prerelease={str(prerelease).lower()}\n")
    print(f"Created {archive.name}")


if __name__ == "__main__":
    main()
