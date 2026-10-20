#!/usr/bin/env python3
"""Run sync, request, export and import against real registries through the normal CLI.

Log in to both registries with skopeo first and set E2E_LOW_REGISTRY and E2E_HIGH_REGISTRY
(E2E_TLS_VERIFY=false for plain HTTP). Use dedicated test registries: the test pushes uniquely
named repositories under E2E_ORG (default mirror). It seeds two multi-platform OCI indexes into the
low registry as sources, so nothing is pulled from the internet. The webhook goes to a listener in
this process, which records each request.
"""
import gzip
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def cli(side, *args, fail=None):
    result = subprocess.run([sys.executable, str(ROOT / 'mirror.py'), '--catalog', str(CATALOG), *map(str, args)],
                            capture_output=True, text=True, check=False, cwd=WORK,
                            env=dict(os.environ, MIRROR_REGISTRY=REGISTRIES[side]))
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', file=sys.stderr, flush=True)
    if fail:
        assert result.returncode != 0 and fail in result.stderr, result.stdout + result.stderr
        print('expected refusal:', fail, flush=True)
    else:
        assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr  # progress is on stderr, data on stdout


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
        mirror.copy(f'oci:{layout}:{version}', 'docker://' + source, destination_side='MIRROR_REGISTRY')
        sources.append(source)
    return sources


def on_high(target, tag):
    """The digest and platforms of target:tag in the high registry."""
    reference = f"docker://{REGISTRIES['high']}/{target}:{tag}"
    raw = json.loads(mirror.run('skopeo', 'inspect', '--raw', *mirror.options('MIRROR_REGISTRY'), reference))
    return mirror.raw_digest(reference, 'MIRROR_REGISTRY'), len(raw.get('manifests', []))


def listener():
    """An HTTP endpoint on localhost standing in for the low NiFi's ListenHTTP: the bodies it received."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (the name http.server calls)
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{server.server_port}/contentListener', received


def tars(directory):
    return sorted(p for p in directory.glob('*.tar'))


if __name__ == '__main__':
    REGISTRIES = {'low': mirror.required_env('E2E_LOW_REGISTRY'), 'high': mirror.required_env('E2E_HIGH_REGISTRY')}
    os.environ['MIRROR_REGISTRY_TLS_VERIFY'] = os.environ.get('E2E_TLS_VERIFY', 'true')
    os.environ['MIRROR_REGISTRY'] = REGISTRIES['low']
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

        os.environ['MIRROR_REQUEST_URL'], requests = listener()
        assert 'requested 1 image tag(s)' in cli('low', 'sync'), 'an image synced before the webhook was set is requested'
        assert [i['repository'] for i in requests[0]['images']] == [target], requests
        assert 'nothing new to request' in cli('low', 'sync')
        assert 'requested 1 image tag(s)' in cli('low', 'request', f'{target}:1.0.0'), 'a named image is requested again'
        assert len(requests) == 2
        del os.environ['MIRROR_REQUEST_URL']
        checks.append('webhook-requests-what-is-new-and-named-resends')

        cli('low', 'export', '-o', out / 'bare.tar', fail='export needs images to copy')
        cli('low', 'export', '--all', '-o', transfer / 'first.tar')

        cli('high', 'import', transfer)
        assert tars(transfer) == [] and not list(transfer.iterdir()), 'the inbox empties once imported'
        digest, platforms = on_high(target, '1.0.0')
        assert digest == mirror.catalog(CATALOG)[0]['digest'] and platforms == 2, (digest, platforms)
        checks.append('import-digest-and-platforms-unchanged')

        CATALOG.write_text('')
        cli('low', 'add', second_source, target)
        cli('low', 'sync')
        cli('low', 'export', '--all', '-o', transfer / 'second.tar', '--rewrite-base-path', rebased.split('/')[0])
        with tarfile.open(transfer / 'second.tar') as archive:
            listed = json.load(archive.extractfile('./export.json'))['images']
        assert [(i['target'], i['tag']) for i in listed] == [(rebased, '1.0.1')], listed
        cli('high', 'import', transfer / 'second.tar')
        assert on_high(rebased, '1.0.1')[0] == mirror.catalog(CATALOG)[0]['digest']
        assert (transfer / 'second.tar').exists(), 'a tar named on its own is kept'
        checks.append('rewrite-base-path')

        cli('high', 'import', transfer / 'second.tar')
        checks.append('reimport-is-a-no-op')

        cli('low', 'export', f'{target}:1.0.1', '-o', out / 'move.tar')
        # Point the high tag elsewhere first: --no-clobber refuses to move it back, a plain import moves it.
        mirror.copy(f"docker://{REGISTRIES['high']}/{target}:1.0.0", f"docker://{REGISTRIES['high']}/{target}:1.0.1",
                    'MIRROR_REGISTRY', 'MIRROR_REGISTRY')
        cli('high', 'import', out / 'move.tar', '--no-clobber', fail='--no-clobber does not move a tag')
        cli('high', 'import', out / 'move.tar')
        assert on_high(target, '1.0.1')[0] == mirror.catalog(CATALOG)[0]['digest']
        checks.append('tag-moves-unless-no-clobber')

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

        assert not list(WORK.rglob('.mirror-staging-*')), 'staging directories are removed'
        checks.append('staging-removed')
        print(json.dumps({'status': 'passed', 'checks': checks}, indent=2))
