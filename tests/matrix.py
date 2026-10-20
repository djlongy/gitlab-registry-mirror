#!/usr/bin/env python3
"""Carry a set of real source references through sync, export and import.

Log in to both registries with skopeo first and set E2E_LOW_REGISTRY, E2E_HIGH_REGISTRY and E2E_ORG.
Each argument is a catalog source: registry/repo:tag in full. Set MIRROR_PLATFORM to check
single-platform transfer.
"""
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGISTRIES = {"low": os.environ["E2E_LOW_REGISTRY"], "high": os.environ["E2E_HIGH_REGISTRY"]}
os.environ.pop("CI_JOB_TOKEN", None)
with tempfile.TemporaryDirectory(prefix="registry-mirror-matrix-") as temporary:
    directory = Path(temporary)
    catalog = directory / "images.txt"
    catalog.touch()
    prefix = os.environ["E2E_ORG"] + "/matrix-" + uuid.uuid4().hex[:8]

    def cli(side, *args):
        result = subprocess.run([sys.executable, str(ROOT / "mirror.py"), "--catalog", str(catalog), *map(str, args)],
                                capture_output=True, text=True, check=False, cwd=directory,
                                env=dict(os.environ, MIRROR_REGISTRY=REGISTRIES[side]))
        assert result.returncode == 0, f"{args}: {result.stderr}"
        return result.stdout

    for number, source in enumerate(sys.argv[1:]):
        cli("low", "add", source, f"{prefix}/{number}/{source.split('@')[0].rsplit(':', 1)[0].rsplit('/', 1)[-1]}")
    cli("low", "sync")
    tar = directory / "matrix.tar"
    cli("low", "export", "--all", "-o", tar)
    cli("high", "import", tar)
    with tarfile.open(tar) as archive:
        entries = json.load(archive.extractfile("./export.json"))["images"]
    os.environ["MIRROR_REGISTRY"] = REGISTRIES["high"]
    for entry in entries:
        high = f"docker://{REGISTRIES['high']}/{entry['target']}:{entry['tag']}"
        raw = json.loads(mirror.run("skopeo", "inspect", "--raw", *mirror.options("MIRROR_REGISTRY"), high))
        assert mirror.raw_digest(high, "MIRROR_REGISTRY") == entry["digest"], entry
        kind = raw.get("mediaType") or raw.get("config", {}).get("mediaType")
        platforms = sorted({f"{m['platform']['os']}/{m['platform']['architecture']}"
                            for m in raw.get("manifests", []) if "platform" in m})
        print(f"PASS {entry['source']}\n     high {entry['digest'][:19]} {kind} {platforms or 'single'}")
    print(f"tar {tar.stat().st_size} bytes, {len(entries)} image(s)")
