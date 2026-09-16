"""Verify release contents and workflow wiring using tracked-file fixtures."""

import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.package_release import COMPONENT, REQUIRED, build_release

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    component = root / COMPONENT
    component.mkdir(parents=True)
    for name in REQUIRED - {"README.md"}:
        target = component / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / COMPONENT / name, target)
    manifest_path = component / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "0.1.1"
    manifest_path.write_text(json.dumps(manifest))
    (root / "README.md").write_text("# Fixture README\n", encoding="utf-8")
    tracked = [str(COMPONENT / name) for name in REQUIRED - {"README.md"}]
    tracked.append("README.md")

    def git_files(args, cwd):
        assert args == [
            "git",
            "ls-files",
            "-z",
            "--",
            COMPONENT.as_posix(),
            "README.md",
        ]
        assert cwd == root
        return "\0".join(tracked).encode()

    monkeypatch.setattr("scripts.package_release.subprocess.check_output", git_files)
    (component / "private.local.json").write_text('{"password":"fixture"}')
    return root


def test_release_contents_checksum_and_reproducibility(release_repo, tmp_path):
    version = json.loads((release_repo / COMPONENT / "manifest.json").read_text())[
        "version"
    ]
    archive, prerelease = build_release(release_repo, f"v{version}", tmp_path / "first")
    assert prerelease is False
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert set(bundle.namelist()) == REQUIRED
        assert bundle.read("README.md") == (release_repo / "README.md").read_bytes()
        assert all(
            stat.S_ISREG(entry.external_attr >> 16) for entry in bundle.infolist()
        )
    assert archive.with_suffix(".zip.sha256").read_text() == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  proxy_inject.zip\n"
    )
    repeated, _ = build_release(release_repo, version, tmp_path / "second")
    assert archive.read_bytes() == repeated.read_bytes()


def test_wrong_tag_does_not_create_output(release_repo, tmp_path):
    with pytest.raises(ValueError, match="Tag must match"):
        build_release(release_repo, "v99.99.99", tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("version", ["0.2.0b1", "0.2.0-rc.1"])
def test_prerelease(release_repo, tmp_path, version):
    path = release_repo / COMPONENT / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = version
    path.write_text(json.dumps(manifest))
    _, prerelease = build_release(release_repo, "v" + version, tmp_path / "out")
    assert prerelease is True


def test_untracked_required_file_rejected(release_repo, tmp_path, monkeypatch):
    tracked = [str(COMPONENT / name) for name in REQUIRED - {"README.md"}]
    monkeypatch.setattr(
        "scripts.package_release.subprocess.check_output",
        lambda *args, **kwargs: "\0".join(tracked).encode(),
    )
    with pytest.raises(ValueError, match="Required release files"):
        build_release(release_repo, "v0.1.1", tmp_path / "out")


def test_external_symlink_rejected(release_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    readme = release_repo / "README.md"
    readme.unlink()
    readme.symlink_to(outside)
    with pytest.raises(ValueError, match="inside repository"):
        build_release(release_repo, "v0.1.1", tmp_path / "out")


def test_hacs_manifest_and_workflow_agree():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads((ROOT / COMPONENT / "manifest.json").read_text())
    assert hacs["zip_release"] and hacs["filename"] == "proxy_inject.zip"
    assert manifest["documentation"] == "https://github.com/xWTF/hass-proxy-inject"
    release = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    checks = yaml.load(
        (ROOT / ".github/workflows/checks.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert release["on"] == {"push": {"tags": ["**"]}}
    assert release["jobs"]["release"]["needs"] == "checks"
    assert release["jobs"]["checks"]["uses"] == "./.github/workflows/checks.yml"
    assert "workflow_call" in checks["on"]
    steps = release["jobs"]["release"]["steps"]
    assert "scripts/package_release.py" in steps[1]["run"]
    assert "proxy_inject.zip.sha256" in steps[2]["run"]
    assert "--prerelease --latest=false" in steps[2]["run"]
