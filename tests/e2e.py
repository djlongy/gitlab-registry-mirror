#!/usr/bin/env python3
"""Run sync, export, send and import against real registries through the normal CLI.

Log in to both registries with skopeo first and set E2E_LOW_REGISTRY and E2E_HIGH_REGISTRY
(E2E_TLS_VERIFY=false for plain HTTP). Use dedicated test registries: the test pushes uniquely
named repositories under E2E_ORG (default mirror). It seeds two multi-platform OCI indexes into the
low registry as sources, so nothing is pulled from the internet.

With S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and E2E_S3_BUCKET set, it also sends
through that bucket and imports from it.
"""
import gzip
import hashlib
import io
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


def cli(side, *args, fail=None):
    result = subprocess.run([sys.executable, str(ROOT / 'mirror.py'), '--catalog', str(CATALOG), *map(str, args)],
                            capture_output=True, text=True, check=False, cwd=WORK,
                            env=dict(os.environ, TARGET_REGISTRY=REGISTRIES[side]))
    print(result.stdout, end='', flush=True)
    if fail:
        assert result.returncode != 0 and fail in result.stderr, result.stdout + result.stderr
        print('expected refusal:', fail, flush=True)
    else:
        assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def seed(target):
    """Two reproducible two-platform OCI indexes, pushed to the low registry as the catalog's sources."""
    sources = []
    for version in ('1.0.0', '1.0.1'):
        layout = WORK / 'seed' / version
        (layout / 'blobs/sha256').mkdir(parents=True)

        def put(data, media_type, layout=layout):
            digest = hashlib.sha256(data).hexdigest()
            (layout / 'blobs/sha256' / digest).write_bytes(data)
            return {'mediaType': media_type, 'size': len(data), 'digest': 'sha256:' + digest}

        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w') as archive:
            item = tarfile.TarInfo('VERSION')
            item.size = len(version)
            archive.addfile(item, io.BytesIO(version.encode()))
        layer = put(gzip.compress(payload.getvalue(), mtime=0), 'application/vnd.oci.image.layer.v1.tar+gzip')
        children = []
        for arch in ('amd64', 'arm64'):
            config = put(json.dumps({'architecture': arch, 'os': 'linux', 'config': {}, 'rootfs': {
                'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(payload.getvalue()).hexdigest()]}}).encode(),
                'application/vnd.oci.image.config.v1+json')
            child = put(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                                    'config': config, 'layers': [layer]}).encode(),
                        'application/vnd.oci.image.manifest.v1+json')
            children.append(child | {'platform': {'architecture': arch, 'os': 'linux'}})
        index = put(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                                'manifests': children}).encode(), 'application/vnd.oci.image.index.v1+json')
        (layout / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
        (layout / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [
            index | {'annotations': {'org.opencontainers.image.ref.name': version}}]}))
        source = f"{REGISTRIES['low']}/{target}-source:{version}"
        mirror.copy(f'oci:{layout}:{version}', 'docker://' + source, destination_side='TARGET_REGISTRY')
        sources.append(source)
    return sources


def on_high(target, tag):
    """The digest and platforms of target:tag in the high registry."""
    reference = f"docker://{REGISTRIES['high']}/{target}:{tag}"
    raw = json.loads(mirror.run('skopeo', 'inspect', '--raw', *mirror.options('TARGET_REGISTRY'), reference))
    return mirror.raw_digest(reference, 'TARGET_REGISTRY'), len(raw.get('manifests', []))


def tars(directory):
    return sorted(p for p in directory.glob('*.tar'))


if __name__ == '__main__':
    REGISTRIES = {'low': mirror.required_env('E2E_LOW_REGISTRY'), 'high': mirror.required_env('E2E_HIGH_REGISTRY')}
    os.environ['TARGET_REGISTRY_TLS_VERIFY'] = os.environ.get('E2E_TLS_VERIFY', 'true')
    os.environ['TARGET_REGISTRY'] = REGISTRIES['low']
    os.environ.pop('CI_JOB_TOKEN', None)  # the ledger is a local file here, even inside a CI job
    with tempfile.TemporaryDirectory(prefix='registry-mirror-e2e-') as temporary:
        WORK = Path(temporary)
        CATALOG = WORK / 'images.txt'
        CATALOG.touch()
        out, transfer = WORK / 'out', WORK / 'transfer'
        out.mkdir()
        transfer.mkdir()
        target = os.environ.get('E2E_ORG', 'mirror') + '/proof-' + uuid.uuid4().hex[:12] + '/image'
        rebased = 'rebased-' + uuid.uuid4().hex[:8] + '/' + target.split('/', 1)[1]
        first_source, second_source = seed(target)
        checks = []

        cli('low', 'add', first_source, target)
        assert 'synced 1 image(s)' in cli('low', 'sync')
        assert '0 tag(s) copied' in cli('low', 'sync'), 'a second sync copies nothing'
        checks.append('sync-idempotent')

        cli('low', 'export', '-o', out / 'first.tar')
        cli('low', 'send', out / 'first.tar', '--path', transfer)
        assert 'nothing new to export' in cli('low', 'export', '-o', out / 'none.tar')
        checks.append('ledger-delta-after-send')

        cli('high', 'import', transfer)
        assert tars(transfer) == [] and not list(transfer.iterdir()), 'the inbox empties once imported'
        digest, platforms = on_high(target, '1.0.0')
        assert digest == mirror.catalog(CATALOG)[0]['digest'] and platforms == 2, (digest, platforms)
        checks.append('import-digest-and-platforms-unchanged')

        CATALOG.write_text('')
        cli('low', 'add', second_source, target)
        cli('low', 'sync')
        cli('low', 'export', '-o', out / 'second.tar', '--rewrite-base-path', rebased.split('/')[0])
        with tarfile.open(out / 'second.tar') as archive:
            listed = json.load(archive.extractfile('./export.json'))['images']
        assert [(i['target'], i['tag']) for i in listed] == [(rebased, '1.0.1')], listed
        cli('low', 'send', out / 'second.tar', '--path', transfer)
        cli('high', 'import', transfer / 'second.tar')
        assert on_high(rebased, '1.0.1')[0] == mirror.catalog(CATALOG)[0]['digest']
        assert (transfer / 'second.tar').exists(), 'a tar named on its own is kept'
        checks.append('delta-and-rewrite-base-path')

        cli('high', 'import', transfer / 'second.tar')
        checks.append('reimport-is-a-no-op')

        cli('low', 'export', f'{target}:1.0.1', '-o', out / 'move.tar')
        # Point the high tag elsewhere first: import must refuse to move it back silently.
        mirror.copy(f"docker://{REGISTRIES['high']}/{target}:1.0.0", f"docker://{REGISTRIES['high']}/{target}:1.0.1",
                    'TARGET_REGISTRY', 'TARGET_REGISTRY')
        cli('high', 'import', out / 'move.tar', fail='does not move without --force')
        cli('high', 'import', out / 'move.tar', '--force')
        assert on_high(target, '1.0.1')[0] == mirror.catalog(CATALOG)[0]['digest']
        checks.append('tag-move-needs-force')

        with (out / 'move.tar').open('ab') as stream:
            stream.write(b'corrupt')
        cli('high', 'import', out / 'move.tar', fail='does not match')
        checks.append('corruption-refused')

        split = out / 'split'
        cli('low', 'export', f'{target}:1.0.1', f'{target}-source:1.0.0', '-o', split / 'carry.tar', '--max-size', '1')
        assert [p.name for p in tars(split)] == ['carry-001-of-002.tar', 'carry-002-of-002.tar'], tars(split)
        cli('high', 'import', split)
        assert on_high(f'{target}-source', '1.0.0')[1] == 2
        checks.append('max-size-split')

        bucket = os.environ.get('E2E_S3_BUCKET')
        if bucket:
            cli('low', 'export', '--all', '-o', out / 's3.tar')
            cli('low', 'send', out / 's3.tar', '--s3-bucket', bucket)
            assert 'imported 1 file(s)' in cli('high', 'import', '--s3-bucket', bucket)
            assert 'nothing to import' in cli('high', 'import', '--s3-bucket', bucket)
            checks.append('s3-send-and-import')

        assert not list(WORK.rglob('.mirror-staging-*')), 'staging directories are removed'
        checks.append('staging-removed')
        print(json.dumps({'status': 'passed', 'checks': checks}, indent=2))
