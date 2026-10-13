"""Catalog, sync, export, send and import contracts, with skopeo and the network mocked."""
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror

LOW = 'low.example.internal'


def blob(directory, data, name='{}'):
    payload = json.dumps(data).encode()
    digest = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name.format(digest)).write_bytes(payload)
    return {'digest': 'sha256:' + digest, 'size': len(payload)}


def image_manifest(directory, name='{}'):
    config = blob(directory, {'architecture': 'amd64', 'os': 'linux'})
    layer = blob(directory, {'test': 'layer'})
    return blob(directory, {'schemaVersion': 2, 'config': config, 'layers': [layer]}, name), layer


def place(stage, directory, root):
    """Name a skopeo dir: copy after its root digest."""
    (directory / 'manifest.json').write_bytes((directory / root['digest'][7:]).read_bytes())
    (directory / root['digest'][7:]).unlink()
    final = stage / 'images' / root['digest'].replace(':', '-')
    final.parent.mkdir(parents=True, exist_ok=True)
    directory.rename(final)
    return final


class FakeGitLab:
    """The generic packages API of project 7, holding the ledger package's uploads."""
    PACKAGES = 'https://git.example/api/v4/projects/7/packages'

    def __init__(self, test):
        self.test, self.uploads, self.deny_delete = test, [], False

    def urlopen(self, request, **_):
        self.test.assertEqual(request.headers.get('Job-token'), 'job')
        url, method = request.full_url.removeprefix(self.PACKAGES), request.get_method()
        newest = [u for u in self.uploads if u['file_name'] == 'ledger.json'][-1:]
        if url == '/generic/registry-mirror-ledger/v2/ledger.json' and method == 'PUT':
            self.uploads.append({'id': max([u['id'] for u in self.uploads] + [9]) + 1, 'file_name': 'ledger.json',
                                 'created_at': mirror.datetime.now(mirror.timezone.utc).isoformat(), 'body': request.data})
            return io.BytesIO(b'')
        if url == '/generic/registry-mirror-ledger/v2/ledger.json' and newest:
            return io.BytesIO(newest[0]['body'])
        if url.startswith('?package_type=generic'):
            return io.BytesIO(json.dumps([{'id': 3, 'name': 'registry-mirror-ledger', 'version': 'v2'},
                                          {'id': 4, 'name': 'registry-mirror-ledger-old', 'version': 'v2'}]).encode())
        if url.startswith('/3/package_files?'):
            return io.BytesIO(json.dumps([{k: v for k, v in u.items() if k != 'body'} for u in self.uploads]).encode())
        if url.startswith('/4/'):
            self.test.fail('another package is never touched')
        if url.startswith('/3/package_files/') and method == 'DELETE' and not self.deny_delete:
            self.uploads = [u for u in self.uploads if u['id'] != int(url.rsplit('/', 1)[1])]
            return io.BytesIO(b'')
        raise mirror.urllib.error.HTTPError(request.full_url, 404 if method == 'GET' else 403, 'No', {}, None)

    def __enter__(self):
        self.patches = [mock.patch.dict(os.environ, {'CI_JOB_TOKEN': 'job', 'CI_API_V4_URL': 'https://git.example/api/v4',
                                                     'CI_PROJECT_ID': '7'}),
                        mock.patch.object(mirror.urllib.request, 'urlopen', side_effect=self.urlopen)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in reversed(self.patches):
            patch.stop()


# A self-signed certificate for CA_BUNDLE tests; no key is needed to trust it.
SELF_SIGNED = '''-----BEGIN CERTIFICATE-----
MIIB4DCCAYWgAwIBAgIUD9HQfIvwfVyrlCwO/Puj5u46j/UwCgYIKoZIzj0EAwIw
JzElMCMGA1UEAwwcbWlycm9yLXRlc3QuZXhhbXBsZS5pbnRlcm5hbDAeFw0yNjEw
MTIwODIwNTlaFw0zNjEwMDkwODIwNTlaMCcxJTAjBgNVBAMMHG1pcnJvci10ZXN0
LmV4YW1wbGUuaW50ZXJuYWwwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASlNz0M
oUmYLl6/y/E8oVNTARRw9Mv9/X6WFYbr8skaQ/Z0RQOTTuR1laMI83jBp35WHwtx
d8CY5LcAW7xV7Slso4GOMIGLMB0GA1UdDgQWBBTZjIOle/IZo1NnERLb9Ki5fgcg
CTAfBgNVHSMEGDAWgBTZjIOle/IZo1NnERLb9Ki5fgcgCTA4BgNVHREEMTAvggls
b2NhbGhvc3SCHG1pcnJvci10ZXN0LmV4YW1wbGUuaW50ZXJuYWyHBH8AAAEwDwYD
VR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNJADBGAiEA+BSTlEeehCz2BGirXusH
Vv66SrqCwzBM/jXHQ+SBHZYCIQCb5HxILz0rk5EbvAFlBOpOI3sNXKzQqCMjVWh5
Y51Iyg==
-----END CERTIFICATE-----
'''


class Registry:
    """A fake pair of registries: tags to digests, and the manifest bytes skopeo would copy."""

    def __init__(self, test):
        self.tags, self.manifests, self.test = {}, {}, test

    def image(self, name):
        raw = json.dumps({'schemaVersion': 2, 'name': name}).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        self.manifests[digest] = raw
        return digest

    def digest(self, reference, _side=None):
        name = reference.removeprefix('docker://')
        if '@' in name:
            return name.rsplit('@', 1)[1]
        if name not in self.tags:
            raise mirror.MirrorError('skopeo inspect failed: manifest unknown')
        return self.tags[name]

    def copy(self, source, destination, *_sides):
        digest = self.digest(source) if source.startswith('docker://') else \
            'sha256:' + hashlib.sha256(Path(source[4:], 'manifest.json').read_bytes()).hexdigest()
        if destination.startswith('dir:'):
            Path(destination[4:]).mkdir(parents=True)
            Path(destination[4:], 'manifest.json').write_bytes(self.manifests[digest])
            Path(destination[4:], 'f' * 64).write_bytes(b'shared layer')
        else:
            self.tags[destination.removeprefix('docker://')] = digest

    def __enter__(self):
        self.patches = [mock.patch.object(mirror, 'raw_digest', side_effect=self.digest),
                        mock.patch.object(mirror, 'copy', side_effect=self.copy)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in self.patches:
            patch.stop()


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.env = mock.patch.dict(os.environ, {'TARGET_REGISTRY': LOW}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for cached in (mirror.ca_files, mirror.cert_dir):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        self.catalog = self.root / 'images.txt'
        self.ledger = self.root.resolve() / '.mirror-ledger.json'
        quiet = mock.patch('sys.stdout', new_callable=io.StringIO)
        self.out = quiet.start()
        self.addCleanup(quiet.stop)
        errors = mock.patch('sys.stderr', new_callable=io.StringIO)
        errors.start()
        self.addCleanup(errors.stop)

    def cli(self, *args):
        return mirror.main(['--catalog', str(self.catalog), *map(str, args)])

    def two_images(self, registry):
        app, db = registry.image('app'), registry.image('db')
        self.catalog.write_text(f'docker.io/team/app:1.0@{app} team/app\ndocker.io/team/db:2@{db} team/db\n')
        return app, db

    def test_ca_bundle_is_trusted_by_skopeo_curl_and_python_on_top_of_the_system_roots(self):
        self.assertEqual(mirror.options('TARGET_REGISTRY'), [])
        bundle = self.root / 'certs' / 'ca-bundle.crt'
        bundle.parent.mkdir()
        bundle.write_text(SELF_SIGNED)
        os.chdir(self.root)
        self.addCleanup(os.chdir, Path(__file__).resolve().parents[1])
        os.environ['CA_BUNDLE'] = './certs/ca-bundle.crt'
        mirror.ca_files.cache_clear()
        _, directory, curl = mirror.ca_files()
        curl = Path(curl[1])
        self.assertEqual(mirror.options('TARGET_REGISTRY', 'dest-', f'docker://{LOW}/team/app:1'),
                         [f'--dest-cert-dir={directory}'])
        # A registry with its own certs.d directory keeps it: CA_BUNDLE is added, never swapped in.
        own = self.root / 'certs.d' / 'reg.example.internal:5000'
        own.mkdir(parents=True)
        (own / 'ca.crt').write_text('their CA')
        (own / 'client.cert').write_text('their client certificate')
        with mock.patch.object(mirror, 'CERTS_D', (self.root / 'missing', self.root / 'certs.d')):
            [flag] = mirror.options(None, 'src-', 'docker://reg.example.internal:5000/team/app:1')
        mixed = Path(flag.split('=', 1)[1])
        self.assertEqual(sorted(p.name for p in mixed.iterdir()), ['ca.crt', 'client.cert', 'mirror-ca-bundle.crt'])
        self.assertEqual((mixed / 'ca.crt').read_text(), 'their CA')
        self.assertEqual((mixed / 'mirror-ca-bundle.crt').read_text(), SELF_SIGNED)
        self.assertEqual(mirror.options(None), [f'--cert-dir={directory}'])
        self.assertEqual((directory / 'ca.crt').read_text(), SELF_SIGNED)
        self.assertTrue(curl.read_text().endswith(SELF_SIGNED))
        system = mirror.ssl.get_default_verify_paths().cafile
        if system and Path(system).is_file():
            self.assertIn(Path(system).read_text().strip()[:200], curl.read_text())
        self.assertGreater(mirror.tls_context().cert_store_stats()['x509_ca'],
                           mirror.ssl.create_default_context().cert_store_stats()['x509_ca'])
        os.environ['TARGET_REGISTRY_TLS_VERIFY'] = 'false'
        self.assertEqual(mirror.options('TARGET_REGISTRY'), ['--tls-verify=false'])
        del os.environ['TARGET_REGISTRY_TLS_VERIFY']
        os.environ['CA_BUNDLE'] = './certs/missing.crt'
        mirror.ca_files.cache_clear()
        with self.assertRaisesRegex(mirror.MirrorError, 'CA_BUNDLE ./certs/missing.crt is not a file'):
            mirror.options('TARGET_REGISTRY')
        self.assertEqual(self.cli('sync'), 1, 'a bad CA_BUNDLE stops every command before it starts')
        self.assertFalse(self.ledger.exists())

    # Ledger

    def test_the_ledger_is_a_local_file_beside_the_catalog_or_the_package_registry_in_ci(self):
        self.assertEqual(mirror.ledger_location(self.catalog), self.ledger)
        with mock.patch.dict(os.environ, {'MIRROR_LEDGER': '/share/ledger.json'}):
            self.assertEqual(mirror.ledger_location(self.catalog), Path('/share/ledger.json'))
        with mock.patch.dict(os.environ, {'CI_JOB_TOKEN': 'x'}):
            self.assertEqual(mirror.ledger_location(self.catalog), 'gitlab')
            with mock.patch.dict(os.environ, {'MIRROR_LEDGER': 'false'}):
                self.assertEqual(mirror.ledger_location(self.catalog), self.ledger)
        self.assertIsNone(mirror.load_ledger(self.ledger, LOW, create=False))
        ledger = mirror.load_ledger(self.ledger, LOW)
        self.assertEqual(ledger['registry'], LOW)
        mirror.save_ledger(self.ledger, ledger)
        with self.assertRaisesRegex(mirror.MirrorError, 'belongs to low.example.internal'):
            mirror.load_ledger(self.ledger, 'other.example.internal')

    def test_the_ci_ledger_reads_and_writes_one_generic_package_file(self):
        gitlab = FakeGitLab(self)
        with gitlab:
            ledger = mirror.load_ledger('gitlab', LOW)
            ledger['sent']['team/app:1.0'] = 'sha256:' + 'a' * 64
            mirror.save_ledger('gitlab', ledger)
            self.assertEqual(mirror.load_ledger('gitlab', LOW)['sent'], ledger['sent'])

    def test_saving_the_ci_ledger_prunes_uploads_past_the_window_but_keeps_the_newest_two(self):
        gitlab = FakeGitLab(self)
        gitlab.uploads = [{'id': n, 'file_name': 'ledger.json', 'created_at': f'2026-09-0{n}T00:00:00.000Z',
                           'body': b'{}'} for n in range(1, 6)]
        gitlab.uploads.append({'id': 9, 'file_name': 'other.json', 'created_at': '2026-09-01T00:00:00.000Z'})
        with gitlab:
            mirror.save_ledger('gitlab', {'registry': LOW})  # today: uploads 1 to 5 are all past 7 days
        self.assertEqual([u['id'] for u in gitlab.uploads], [5, 9, 10])  # 10 and 5 are the newest two
        with gitlab, mock.patch.dict(os.environ, {'MIRROR_LEDGER_KEEP_DAYS': '100000'}):
            mirror.save_ledger('gitlab', {'registry': LOW})
        self.assertEqual(len(gitlab.uploads), 4)
        gitlab.deny_delete = True
        with gitlab, mock.patch.object(sys, 'stderr', io.StringIO()) as stderr:
            mirror.save_ledger('gitlab', {'registry': LOW})
        self.assertIn('not pruned: DELETE', stderr.getvalue())
        self.assertEqual(len(gitlab.uploads), 5)

    def test_a_ledger_url_gives_a_manual_export_the_pipelines_delta_and_is_never_written(self):
        url = 'https://git.example.internal/api/v4/projects/7/packages/generic/registry-mirror-ledger/v2/ledger.json'
        requests = []

        def urlopen(request, **_):
            requests.append((request.get_method(), request.full_url, request.headers.get('Private-token')))
            return io.BytesIO(json.dumps(pipeline).encode())

        with Registry(self) as registry:
            app, db = self.two_images(registry)
            self.cli('sync')
            pipeline = json.loads(self.ledger.read_text())
            pipeline['sent'] = {'team/app:1.0': app}  # the pipeline already sent app
            os.environ.update(PACKAGE_TOKEN='token', MIRROR_LEDGER=url)
            transfer = self.root / 'transfer'
            transfer.mkdir()
            with mock.patch.object(mirror.urllib.request, 'urlopen', side_effect=urlopen):
                self.assertEqual(self.cli('export', '-o', self.root / 'manual.tar'), 0)
                self.assertEqual(self.tar_images(self.root / 'manual.tar'), [(f'{LOW}/team/db:2', 'team/db', '2')])
                self.assertEqual(self.cli('send', self.root / 'manual.tar', '--path', transfer), 0)
                self.assertEqual(self.cli('sync'), 0)
        self.assertEqual({r[0] for r in requests}, {'GET'})
        self.assertEqual({r[1:] for r in requests}, {(url, 'token')})
        os.environ['MIRROR_LEDGER'] = url + '-missing'
        with mock.patch.object(mirror.urllib.request, 'urlopen', side_effect=mirror.urllib.error.HTTPError(
                url, 404, 'Not Found', {}, None)):
            self.assertEqual(self.cli('export'), 1)

    # Sync

    def test_sync_copies_by_digest_records_the_ledger_and_is_idempotent(self):
        with Registry(self) as registry:
            app, db = self.two_images(registry)
            self.assertEqual(self.cli('sync'), 0)
            self.assertEqual(registry.tags, {f'{LOW}/team/app:1.0': app, f'{LOW}/team/db:2': db})
            ledger = json.loads(self.ledger.read_text())
            self.assertEqual(ledger['synced']['team/app:1.0'], {'pin': app, 'digest': app, 'tags': ['1.0']})
            with mock.patch.object(mirror, 'copy') as again:
                self.assertEqual(self.cli('sync'), 0)
            again.assert_not_called()
            del registry.tags[f'{LOW}/team/db:2']  # deleted or expired: the next run puts it back
            self.assertEqual(self.cli('sync'), 0)
            self.assertIn(f'{LOW}/team/db:2', registry.tags)

    def webhook(self, registry, token=None, bundle=None, error=None):
        """Run sync with MIRROR_WEBHOOK_URL set; curl is mocked. Returns the curl calls and the payload sent."""
        os.environ['MIRROR_WEBHOOK_URL'] = 'https://nifi.example.internal:9099/contentListener'
        if token:
            os.environ['MIRROR_WEBHOOK_TOKEN'] = token
        if bundle:
            os.environ['MIRROR_WEBHOOK_CA_BUNDLE'] = str(bundle)
        sent = []

        def fake(*args):
            if args[0] != 'curl':
                return real(*args)
            if error:
                raise mirror.MirrorError(error)
            path = Path(args[args.index('--upload-file') + 1])
            headers = [args[i + 1] for i, a in enumerate(args) if a == '--header']
            sent.append((args, headers, json.loads(path.read_text()),
                         [Path(h[1:]).read_text() for h in headers if h.startswith('@')]))
            return b'200'

        real = mirror.run
        with mock.patch.object(mirror, 'run', side_effect=fake):
            code = self.cli('sync')
        return code, sent

    def test_webhook_posts_once_after_a_sync_that_changed_images_as_a_send_delivery(self):
        with Registry(self) as registry:
            app, db = self.two_images(registry)
            code, sent = self.webhook(registry, token='s3cret')
            self.assertEqual(code, 0)
            [(args, headers, body, auth)] = sent
            self.assertEqual(body['registry'], LOW)
            self.assertEqual(body['images'], [{'repository': 'team/app', 'tag': '1.0', 'digest': app},
                                              {'repository': 'team/db', 'tag': '2', 'digest': db}])
            self.assertEqual(body['references'], 'team/app:1.0\nteam/db:2')
            for header in ('Content-Type: application/json', 'X-Artifact-Type: container-images-request',
                           'X-Artifact-Format: json', 'Filename: container-images-request.json', 'Expect:'):
                self.assertIn(header, headers)
            self.assertTrue(any(h.startswith('X-Sha256: ') for h in headers))
            self.assertEqual(auth, ['Authorization: Bearer s3cret\n'])  # from a file, not the command line
            self.assertNotIn('s3cret', ' '.join(args) + self.out.getvalue() + sys.stderr.getvalue())
            self.assertIn('--max-time', args)
            self.assertEqual(self.out.getvalue().count('posted to NiFi: container-images-request.json'), 1)

    def test_webhook_is_silent_when_nothing_changed_or_unset(self):
        with Registry(self) as registry:
            self.two_images(registry)
            self.assertEqual(self.cli('sync'), 0)
            code, sent = self.webhook(registry)  # second run: every tag already there
            self.assertEqual((code, sent), (0, []))
            del os.environ['MIRROR_WEBHOOK_URL']
            with mock.patch.object(mirror, 'run', side_effect=AssertionError('no curl')):
                mirror.notify(LOW, [{'repository': 'a', 'tag': '1', 'digest': 'sha256:0'}])  # unset: no post

    def test_webhook_trusts_its_ca_bundle_on_top_of_the_system_roots(self):
        bundle = self.root / 'webhook-ca.pem'
        bundle.write_text(SELF_SIGNED)
        with Registry(self) as registry:
            self.two_images(registry)
            code, sent = self.webhook(registry, bundle=bundle)
        self.assertEqual(code, 0)
        args = sent[0][0]
        pem = Path(args[args.index('--cacert') + 1])
        self.assertIn(SELF_SIGNED, pem.read_text())

    def test_webhook_failure_only_warns(self):
        with Registry(self) as registry:
            self.two_images(registry)
            code, sent = self.webhook(registry, token='s3cret', error='curl POST failed: HTTP 503')
            self.assertEqual(code, 0)
            self.assertIn('warning: webhook not delivered', sys.stderr.getvalue())
            self.assertNotIn('s3cret', sys.stderr.getvalue())
            self.assertEqual(len(registry.tags), 2)

    def test_latest_gets_its_version_tag_but_never_one_the_catalog_pins_or_pinned(self):
        with Registry(self) as registry, mock.patch.object(mirror, 'app_version', return_value='8.10.2'):
            latest, pinned = registry.image('latest'), registry.image('pinned')
            self.catalog.write_text(f'docker.io/bitnami/redis:latest@{latest} team/redis\n')
            self.assertEqual(self.cli('sync'), 0)
            self.assertEqual(registry.tags[f'{LOW}/team/redis:8.10.2'], latest)
            self.catalog.write_text(f'docker.io/bitnami/redis:latest@{latest} team/redis\n'
                                    f'docker.io/bitnami/redis:9.0.0@{pinned} team/redis\n')
            with mock.patch.object(mirror, 'app_version', return_value='9.0.0'):
                self.assertEqual(self.cli('sync'), 0)
                self.assertEqual(registry.tags[f'{LOW}/team/redis:9.0.0'], pinned)
                # The pin's line is removed; latest still does not take the tag the pin established.
                self.catalog.write_text(f'docker.io/bitnami/redis:latest@{latest} team/redis\n')
                ledger = json.loads(self.ledger.read_text())
                ledger['platforms'] = {}
                self.ledger.write_text(json.dumps(ledger))
                self.assertEqual(self.cli('sync'), 0)
            self.assertEqual(registry.tags[f'{LOW}/team/redis:9.0.0'], pinned)

    def test_pending_lists_what_sync_has_not_copied(self):
        with Registry(self) as registry:
            app, db = self.two_images(registry)
            self.assertEqual(self.cli('pending'), 0)
            self.assertEqual(self.out.getvalue().splitlines(), [f'docker.io/team/app:1.0@{app}', f'docker.io/team/db:2@{db}'])
            self.cli('sync')
            self.out.truncate(0)
            self.out.seek(0)
            self.cli('pending')
            self.assertEqual(self.out.getvalue(), '')
            os.environ['MIRROR_PLATFORM'] = 'linux/amd64'  # a platform change syncs again
            self.cli('pending')
            self.assertEqual(len(self.out.getvalue().splitlines()), 2)

    # Export, send and import

    def tar_images(self, tar):
        with tarfile.open(tar) as archive:
            return [(i['source'], i['target'], i['tag']) for i in json.load(archive.extractfile('./export.json'))['images']]

    def test_export_takes_what_is_new_since_the_last_send(self):
        transfer = self.root / 'transfer'
        transfer.mkdir()
        with Registry(self) as registry:
            app, db = self.two_images(registry)
            self.assertEqual(self.cli('export', '-o', self.root / 'none.tar'), 0)
            self.assertIn('not synced yet, run sync first: team/app:1.0', self.out.getvalue())
            self.assertFalse((self.root / 'none.tar').exists())
            self.cli('sync')
            first = self.root / 'first.tar'
            self.assertEqual(self.cli('export', '-o', first), 0)
            self.assertEqual(self.tar_images(first), [(f'{LOW}/team/app:1.0', 'team/app', '1.0'),
                                                      (f'{LOW}/team/db:2', 'team/db', '2')])
            # Not sent yet: the next export takes the same images again.
            self.assertEqual(self.cli('export', '-o', self.root / 'again.tar'), 0)
            self.assertEqual(len(self.tar_images(self.root / 'again.tar')), 2)
            self.assertEqual(self.cli('send', first, '--path', transfer), 0)
            self.assertEqual(json.loads(self.ledger.read_text())['sent'], {'team/app:1.0': app, 'team/db:2': db})
            self.assertEqual(self.cli('export', '-o', self.root / 'quiet.tar'), 0)
            self.assertIn('nothing new to export', self.out.getvalue())
            self.assertFalse((self.root / 'quiet.tar').exists())
            new = registry.image('app-1.1')
            self.catalog.write_text(f'docker.io/team/app:1.1@{new} team/app\ndocker.io/team/db:2@{db} team/db\n')
            self.cli('sync')
            self.assertEqual(self.cli('export', '-o', self.root / 'delta.tar'), 0)
            self.assertEqual(self.tar_images(self.root / 'delta.tar'), [(f'{LOW}/team/app:1.1', 'team/app', '1.1')])
            self.assertEqual(self.cli('export', '--all', '-o', self.root / 'all.tar'), 0)
            self.assertEqual(len(self.tar_images(self.root / 'all.tar')), 2)

    def test_export_names_images_from_args_a_list_or_any_registry_and_rewrites_the_base_path(self):
        with Registry(self) as registry:
            registry.tags[f'{LOW}/team/app:1.0'] = registry.image('app')
            registry.tags['other.example.com/team/web:3'] = registry.image('web')
            listing = self.root / 'carry.txt'
            listing.write_text('# carry these\nother.example.com/team/web:3\n')
            tar = self.root / 'named.tar'
            self.assertEqual(self.cli('export', 'team/app:1.0', '--list', listing, '-o', tar,
                                      '--rewrite-base-path', 'new-org-dev'), 0)
            self.assertEqual(self.tar_images(tar), [(f'{LOW}/team/app:1.0', 'new-org-dev/app', '1.0'),
                                                    ('other.example.com/team/web:3', 'new-org-dev/web', '3')])
            self.assertEqual(self.cli('export', 'team/app:1.0', '-o', tar), 1)  # never overwrites
            self.assertEqual(mirror.rebase('team/prom/prometheus', 'new-org'), 'new-org/prom/prometheus')
            self.assertEqual(mirror.rebase('team/app', None), 'team/app')
            with self.assertRaises(mirror.MirrorError):
                mirror.rebase('team/app', 'Bad Org')

    def test_max_size_splits_whole_images_into_several_tars_and_none_means_one(self):
        with Registry(self) as registry, mock.patch.object(mirror, 'image_bytes', side_effect=[3, 3, 9]):
            for name in ('a', 'b', 'c'):
                registry.tags[f'{LOW}/team/{name}:1'] = registry.image(name)
            self.assertEqual(self.cli('export', 'team/a:1', 'team/b:1', 'team/c:1', '-o', self.root / 'carry.tar',
                                      '--max-size', '6'), 0)
            self.assertEqual(sorted(p.name for p in self.root.glob('carry-*.tar')),
                             ['carry-001-of-002.tar', 'carry-002-of-002.tar'])
            self.assertEqual(len(self.tar_images(self.root / 'carry-001-of-002.tar')), 2)
            self.assertIn('over --max-size', self.out.getvalue())
            self.assertEqual(self.cli('export', 'team/a:1', 'team/b:1', 'team/c:1', '-o', self.root / 'whole.tar'), 0)
            self.assertEqual(len(self.tar_images(self.root / 'whole.tar')), 3)
        self.assertEqual(mirror.size('4GiB'), 4 * 1024 ** 3)
        with self.assertRaises(mirror.MirrorError):
            mirror.size('4GB')

    def test_staging_is_beside_the_tar_or_overridden_and_always_removed(self):
        staging = self.root / 'big-disk'
        with Registry(self) as registry:
            registry.tags[f'{LOW}/team/app:1.0'] = registry.image('app')
            with mock.patch.object(mirror, 'copy', side_effect=mirror.MirrorError('registry down')):
                self.assertEqual(self.cli('export', 'team/app:1.0', '-o', self.root / 'out' / 'x.tar',
                                          '--staging-dir', staging), 1)
            self.assertEqual(list(staging.iterdir()), [])
            self.assertEqual(list((self.root / 'out').iterdir()), [])
            seen = []
            real = registry.copy

            def watch(source, destination, *sides):
                seen.append((Path(destination[4:]).parent.parent.parent, os.environ['TMPDIR']))
                real(source, destination, *sides)
            with mock.patch.object(mirror, 'copy', side_effect=watch):
                self.assertEqual(self.cli('export', 'team/app:1.0', '-o', self.root / 'out' / 'x.tar'), 0)
            self.assertEqual(seen[0][0], self.root / 'out')
            self.assertTrue(seen[0][1].startswith(str(self.root / 'out' / '.mirror-staging-')))
            self.assertEqual(sorted(p.name for p in (self.root / 'out').iterdir()), ['x.tar', 'x.tar.sha256'])
        self.assertNotIn('TMPDIR', os.environ)

    def exported(self, registry, name='carry.tar', source=f'{LOW}/team/app:1.0'):
        registry.tags[source] = registry.image(source)
        tar = self.root / name
        self.assertEqual(self.cli('export', source, '-o', tar), 0)
        return tar, registry.tags[source]

    def test_import_pushes_digests_unchanged_skips_present_and_never_moves_a_tag_without_force(self):
        with Registry(self) as registry:
            tar, digest = self.exported(registry)
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            record = self.root / 'imported.json'
            self.assertEqual(self.cli('import', tar, '--record', record, '--rewrite-base-path', 'org-dev'), 0)
            self.assertEqual(registry.tags['high.example.internal/org-dev/app:1.0'], digest)
            self.assertEqual(json.loads(record.read_text()), [{'target': 'org-dev/app', 'tag': '1.0', 'digest': digest}])
            self.assertTrue(tar.exists())  # a named tar is kept
            with mock.patch.object(mirror, 'copy') as push:
                self.assertEqual(self.cli('import', tar, '--rewrite-base-path', 'org-dev'), 0)
            push.assert_not_called()
            registry.tags['high.example.internal/team/app:1.0'] = 'sha256:' + 'b' * 64
            self.assertEqual(self.cli('import', tar), 1)
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], 'sha256:' + 'b' * 64)
            self.assertEqual(self.cli('import', tar, '--force'), 0)
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], digest)
        with mock.patch.object(mirror, 'raw_digest', side_effect=mirror.MirrorError('skopeo inspect failed: unauthorized')), \
                mock.patch.object(mirror, 'copy') as push:
            self.assertEqual(self.cli('import', tar), 1)  # not read as an absent tag
        push.assert_not_called()

    def test_import_refuses_a_tar_without_a_matching_checksum(self):
        tar = self.root / 'carry.tar'
        tar.write_bytes(b'not the exported bytes')
        with self.assertRaisesRegex(mirror.MirrorError, 'sha256 is missing'):
            mirror.import_tar(tar)
        tar.with_name('carry.tar.sha256').write_text('0' * 64 + '  carry.tar\n')
        with self.assertRaisesRegex(mirror.MirrorError, 'does not match'):
            mirror.import_tar(tar)

    def test_a_directory_is_an_inbox_and_imported_tars_leave_it(self):
        inbox = self.root / 'received'
        inbox.mkdir()
        with Registry(self) as registry:
            tar, digest = self.exported(registry)
            half, _ = self.exported(registry, 'half.tar', f'{LOW}/team/db:2')
            for path in (tar, tar.with_name('carry.tar.sha256'), half):
                os.replace(path, inbox / path.name)
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.cli('import', inbox, '--keep'), 0)
            self.assertTrue((inbox / 'carry.tar').exists())
            self.assertEqual(self.cli('import', inbox), 0)
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), ['half.tar'])  # no .sha256 yet: waits
            self.assertIn('waiting for half.tar.sha256', self.out.getvalue())
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], digest)

    def test_strip_base_path_removes_the_first_segment_on_export_or_import(self):
        self.assertEqual(mirror.rebase('team/prom/prometheus', mirror.STRIP), 'prom/prometheus')
        self.assertEqual(mirror.rebase('team/app', mirror.STRIP), 'app')
        self.assertEqual(mirror.rebase('team/app', 'new-org'), 'new-org/app')
        self.assertEqual(mirror.rebase('team/app', ''), 'team/app')
        with Registry(self) as registry:
            tar, digest = self.exported(registry)
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.cli('import', tar, '--strip-base-path'), 0)
            self.assertEqual(registry.tags['high.example.internal/app:1.0'], digest)
            os.environ['MIRROR_REWRITE_BASE_PATH'] = 'org'
            with self.assertRaises(SystemExit):  # both at once is refused
                self.cli('import', tar, '--strip-base-path')

    def test_import_no_checksum_takes_a_tar_without_its_sha256_but_still_checks_one_that_is_there(self):
        with Registry(self) as registry:
            tar, digest = self.exported(registry)
            sidecar = tar.with_name('carry.tar.sha256')
            good = sidecar.read_text()
            sidecar.unlink()
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.cli('import', tar), 1)
            inbox = self.root / 'inbox'
            inbox.mkdir()
            shutil.copyfile(tar, inbox / 'carry.tar')
            self.assertEqual(self.cli('import', inbox), 0)  # waits for the .sha256
            self.assertEqual(list(inbox.iterdir()), [inbox / 'carry.tar'])
            self.assertEqual(self.cli('import', inbox, '--no-checksum'), 0)
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], digest)
            self.assertEqual(list(inbox.iterdir()), [])
            self.assertEqual(self.cli('import', tar, '--no-checksum'), 0)
            sidecar.write_text('0' * 64 + '  carry.tar\n')
            self.assertEqual(self.cli('import', tar, '--no-checksum'), 1)  # present and wrong: refused
            sidecar.write_text(good)

    def test_send_needs_one_destination_that_exists_and_writes_the_checksum_last(self):
        with Registry(self) as registry:
            tar, _ = self.exported(registry)
        self.assertEqual(self.cli('send', tar, '--path', self.root / 'not-mounted'), 1)
        with self.assertRaises(SystemExit):
            self.cli('send', tar, '--path', self.root, '--s3-bucket', 'b')
        transfer = self.root / 'transfer'
        transfer.mkdir()
        order = []
        real = os.replace
        with mock.patch.object(mirror.os, 'replace',
                               side_effect=lambda a, b: (order.append((Path(a).name, Path(b).name)), real(a, b))):
            self.assertEqual(self.cli('send', tar, '--path', transfer), 0)
        # Written under a hidden name, which NiFi ListFile and GetFile skip by default, then renamed.
        self.assertEqual(order[-2:], [('.carry.tar.partial', 'carry.tar'),
                                      ('.carry.tar.sha256.partial', 'carry.tar.sha256')])
        self.assertEqual(sorted(p.name for p in transfer.iterdir()), ['carry.tar', 'carry.tar.sha256'])
        tar.with_name('carry.tar.sha256').write_text('0' * 64 + '  carry.tar\n')
        self.assertEqual(self.cli('send', tar, '--path', transfer), 1)  # checked before anything is sent

    def test_send_to_nifi_posts_the_tar_then_its_checksum_streamed_without_credentials_in_the_log(self):
        with Registry(self) as registry:
            tar, _ = self.exported(registry)
        url = 'http://user:secret@nifi.example.internal:9099/contentListener'
        with mock.patch.object(mirror, 'run', return_value=b'200') as call:
            self.assertEqual(self.cli('send', tar, '--nifi-url', url), 0)
        posted = [c.args for c in call.call_args_list]
        self.assertEqual([args[args.index('--upload-file') + 1] for args in posted], [str(tar), str(tar) + '.sha256'])
        headers = [posted[0][i + 1] for i, a in enumerate(posted[0]) if a == '--header']
        for header in ('Filename: carry.tar', 'Content-Type: application/octet-stream', f'X-Sha256: {mirror.sha256(tar)}',
                       'X-Artifact-Type: container-images', 'X-Artifact-Format: tar', 'Expect:'):
            self.assertIn(header, headers)
        self.assertIn('--http1.1', posted[0])
        self.assertNotIn('--data-binary', posted[0])
        self.assertIn('posted to NiFi: carry.tar -> http://nifi.example.internal:9099/contentListener', self.out.getvalue())
        self.assertNotIn('secret', self.out.getvalue())

    def test_send_flowfile_writes_one_package_with_the_http_attributes_and_import_reads_it(self):
        with Registry(self) as registry:
            tar, digest = self.exported(registry)
            transfer = self.root / 'transfer'
            transfer.mkdir()
            self.assertEqual(self.cli('send', tar, '--path', transfer, '--flowfile'), 0)
            self.assertEqual([p.name for p in transfer.iterdir()], ['carry.tar.ffv3'])
            unpacked = self.root / 'unpacked'
            unpacked.mkdir()
            # One FlowFile: the tar, its checksum an attribute, so the link carries one file.
            self.assertEqual(mirror.read_flowfiles(transfer / 'carry.tar.ffv3', unpacked), [
                (unpacked / 'carry.tar', {'filename': 'carry.tar', **mirror.attributes(tar, mirror.sha256(tar))})])
            self.assertEqual(list(mirror.attributes(tar, '')), ['Filename', 'Content-Type', 'X-Sha256', 'X-Artifact-Type',
                                                                'X-Artifact-Format'])
            self.assertEqual((unpacked / 'carry.tar').read_bytes(), tar.read_bytes())
            self.assertEqual(self.cli('send', tar, '--nifi-url', 'http://nifi.example.internal', '--flowfile'), 1)
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.cli('import', transfer), 0)
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], digest)
            self.assertEqual(list(transfer.iterdir()), [])
        # A bad package in the inbox stays put and does not hold up a good one behind it.
        mirror.write_flowfiles(transfer / 'a-bad.ffv3', [(tar, {'filename': 'carry.tar', 'X-Sha256': '0' * 64})])
        with Registry(self) as registry:
            self.assertEqual(self.cli('send', tar, '--path', transfer, '--flowfile'), 0)
            registry.tags.clear()
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.cli('import', transfer), 1)
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], digest)
        self.assertEqual([p.name for p in transfer.iterdir()], ['a-bad.ffv3'])
        # The sender refuses a name the far side would refuse, and a failed write leaves nothing.
        odd = tar.with_name('my carry.tar')
        shutil.copyfile(tar, odd)
        shutil.copyfile(tar.with_name('carry.tar.sha256'), odd.with_name('my carry.tar.sha256'))
        os.environ['TARGET_REGISTRY'] = LOW
        self.assertEqual(self.cli('send', odd, '--path', transfer, '--flowfile'), 1)
        with self.assertRaises(OSError):
            mirror.land(transfer / 'x.ffv3', lambda hidden: (hidden.write_bytes(b'half'), open('/nonexistent/x')))
        self.assertEqual([p.name for p in transfer.iterdir()], ['a-bad.ffv3'])
        huge = self.root / 'huge.ffv3'
        huge.write_bytes(mirror.FLOWFILE_MAGIC + (1).to_bytes(2, 'big') + b'\xff\xff' + (1 << 30).to_bytes(4, 'big'))
        with self.assertRaisesRegex(mirror.MirrorError, 'attribute of 1073741824 bytes'):
            mirror.read_flowfiles(huge, unpacked)
        evil = self.root / 'evil.ffv3'
        mirror.write_flowfiles(evil, [(tar, {'filename': '../escape.tar'})])
        with self.assertRaisesRegex(mirror.MirrorError, 'unsafe filename'):
            mirror.read_flowfiles(evil, unpacked)
        mirror.write_flowfiles(evil, [(tar, {'filename': 'carry.tar'})])
        evil.write_bytes(evil.read_bytes()[:-5])
        with self.assertRaisesRegex(mirror.MirrorError, 'truncated'):
            mirror.read_flowfiles(evil, unpacked)

    def s3_env(self):
        os.environ.update({'S3_ENDPOINT': 'https://s3.example.internal:9000', 'AWS_ACCESS_KEY_ID': 'AKID',
                           'AWS_SECRET_ACCESS_KEY': 'secret', 'S3_PREFIX': 'mirror'})

    def test_s3_send_and_import_sign_path_style_requests_and_list_the_bucket(self):
        self.s3_env()
        bucket, calls, metadata = {}, [], {}

        def urlopen(request, **_):
            parts = mirror.urllib.parse.urlsplit(request.full_url)
            calls.append((request.get_method(), parts.path, parts.query))
            self.assertIn('AWS4-HMAC-SHA256 Credential=AKID/', request.headers['Authorization'])
            self.assertIn('x-amz-content-sha256', request.headers['Authorization'])
            key = mirror.urllib.parse.unquote(parts.path.split('/', 2)[2]) if parts.path.count('/') > 1 else ''
            method = request.get_method()
            if method == 'PUT':
                bucket[key] = request.data.read()
                metadata[key] = {k.lower()[len('x-amz-meta-'):]: v for k, v in request.headers.items()
                                 if k.lower().startswith('x-amz-meta-')}
                signed = request.headers['Authorization'].split('SignedHeaders=')[1].split(',')[0].split(';')
                self.assertTrue({'content-type', *(f'x-amz-meta-{k}' for k in metadata[key])} <= set(signed))
                metadata[key]['content-type'] = request.headers['Content-type']
                self.assertEqual(request.headers['Content-length'], str(len(bucket[key])))
            elif method == 'DELETE':
                del bucket[key]
            elif not key:
                self.assertIn('list-type=2', parts.query)
                self.assertIn('prefix=mirror%2F', parts.query)
                items = ''.join(f'<Contents><Key>{k}</Key></Contents>' for k in sorted(bucket))
                return io.BytesIO(f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}'
                                  '<IsTruncated>false</IsTruncated></ListBucketResult>'.encode())
            else:
                return io.BytesIO(bucket[key])
            return io.BytesIO(b'')

        with Registry(self) as registry, mock.patch.object(mirror.urllib.request, 'urlopen', side_effect=urlopen):
            tar, digest = self.exported(registry)
            self.assertEqual(self.cli('send', tar, '--s3-bucket', 'transfer'), 0)
            self.assertEqual(sorted(bucket), ['mirror/carry.tar', 'mirror/carry.tar.sha256'])
            self.assertEqual([c[1] for c in calls], ['/transfer/mirror/carry.tar', '/transfer/mirror/carry.tar.sha256'])
            # The HTTP header names and values; S3 keeps metadata names in lowercase.
            self.assertEqual(metadata['mirror/carry.tar'],
                             {k.lower(): v for k, v in mirror.attributes(tar, mirror.sha256(tar)).items()})
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            os.chdir(self.root)
            self.assertEqual(self.cli('import', '--s3-bucket', 'transfer'), 0)
            self.assertEqual(registry.tags['high.example.internal/team/app:1.0'], digest)
            self.assertEqual(bucket, {})
            # A .ffv3 in the bucket keeps the attribute names case for case.
            os.environ['TARGET_REGISTRY'] = LOW
            self.assertEqual(self.cli('send', tar, '--s3-bucket', 'transfer', '--flowfile'), 0)
            self.assertEqual(sorted(bucket), ['mirror/carry.tar.ffv3'])
            os.environ['TARGET_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.cli('import', '--s3-bucket', 'transfer'), 0)
            self.assertEqual(bucket, {})
            self.assertEqual([p.name for p in self.root.glob('.mirror-staging-*')], [])

    def test_variables_from_an_older_release_fail_with_what_replaces_them(self):
        os.environ.update({'LOW_QUAY_HOST': 'low.example.internal', 'RESEND_SEQUENCE': '3..', 'MIRROR_SEND': 'nifi'})
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(mirror.main(['targets']), 1)
        said = err.getvalue()
        self.assertIn('LOW_QUAY_HOST is replaced by TARGET_REGISTRY', said)
        self.assertIn('RESEND_SEQUENCE is replaced by EXPORT_IMAGES', said)
        self.assertIn('MIRROR_SEND is replaced by NIFI_URL, S3_BUCKET or MIRROR_SEND_PATH', said)

    # Catalog, add, platforms and promote

    def test_catalog_requires_a_digest_and_an_org_and_repository(self):
        pinned = 'docker.io/library/alpine:3.20@sha256:' + 'a'*64
        self.assertEqual(mirror.parse_image(pinned, 'team-dev/library/alpine')['target'], 'team-dev/library/alpine')
        for source, target in [('docker.io/library/alpine:3.20', 'mirror/alpine'),
                               (pinned, 'alpine'), (pinned, 'mirror/../alpine'), (pinned, 'mirror//alpine'),
                               ('docker.io/../alpine:3.20@sha256:' + 'a'*64, 'mirror/alpine')]:
            with self.subTest(source=source, target=target), self.assertRaises(mirror.MirrorError):
                mirror.parse_image(source, target)

    def test_duplicate_targets_fail(self):
        path = self.root / 'images.txt'
        path.write_text(('docker.io/library/alpine:3.20@sha256:' + 'a'*64 + ' mirror/alpine\n') * 2)
        with self.assertRaisesRegex(mirror.MirrorError, 'duplicate'):
            mirror.catalog(path)

    def test_add_resolves_a_pin(self):
        path = self.root / 'images.txt'
        image = json.dumps({'schemaVersion': 2, 'config': {'mediaType': 'application/vnd.oci.image.config.v1+json'},
                            'layers': []}).encode()
        index = json.dumps({'schemaVersion': 2, 'manifests': [
            {'digest': 'sha256:' + hashlib.sha256(image).hexdigest(),
             'platform': {'os': 'linux', 'architecture': 'amd64'}}]}).encode()
        pinned = 'sha256:' + hashlib.sha256(index).hexdigest()
        with mock.patch.object(mirror, 'run', side_effect=[index, image]) as lookup:
            mirror.add_image(path, 'docker.io/prom/prometheus:v3.13.4', 'team-dev/prom/prometheus')
        self.assertEqual(lookup.call_args_list[0].args[-1], 'docker://docker.io/prom/prometheus:v3.13.4')
        self.assertEqual(path.read_text(), f'docker.io/prom/prometheus:v3.13.4@{pinned} team-dev/prom/prometheus\n')
        self.assertEqual(mirror.normalise('alpine:3.20'), 'docker.io/library/alpine:3.20')
        for short, full in [('prom/prometheus:v3.13.4', 'docker.io/prom/prometheus:v3.13.4'),
                            ('alpine:3.20', 'docker.io/library/alpine:3.20')]:
            with self.assertRaisesRegex(mirror.MirrorError, f'no registry host; give the full reference, for example {full}'):
                mirror.add_image(path, short, 'team-dev/prom/prometheus')
            with self.assertRaises(mirror.MirrorError):
                mirror.parse_image(short + '@sha256:' + 'a'*64, 'team-dev/prom/prometheus')
        self.assertEqual(mirror.parse_image('registry:5000/a/b:1@sha256:' + 'a'*64, 'team/b')['tag'], '1')
        self.assertEqual(mirror.normalise('quay.io/org/image:1'), 'quay.io/org/image:1')
        self.assertEqual(mirror.normalise('localhost:5000/image:1'), 'localhost:5000/image:1')

    def test_add_refuses_signatures_and_metadata_artifacts(self):
        image_config = {'mediaType': 'application/vnd.docker.container.image.v1+json'}
        self.assertTrue(mirror.runnable({'schemaVersion': 2, 'config': image_config, 'layers': []}))
        # cosign's .sig tag: an image config media type, an empty {} config and a simplesigning layer.
        cosign = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                  'config': {'mediaType': 'application/vnd.oci.image.config.v1+json', 'size': 2,
                             'digest': 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                  'layers': [{'mediaType': 'application/vnd.dev.cosign.simplesigning.v1+json',
                              'digest': 'sha256:' + 'ab'*32, 'size': 251}]}
        for artifact in ({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + 'b'*64,
                                                             'artifactType': 'application/vnd.cncf.notary.signature'}]},
                         {'schemaVersion': 2, 'artifactType': 'application/vnd.example.metadata.config.v1+json',
                          'config': {'mediaType': 'application/vnd.oci.empty.v1+json'}, 'layers': []},
                         {'schemaVersion': 2, 'config': {'mediaType': 'application/vnd.dev.cosign.simplesigning.v1+json'}},
                         cosign,
                         {'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + 'b'*64,
                          'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                          'platform': {'os': 'unknown', 'architecture': 'unknown'}}]}):
            with self.subTest(artifact=artifact):
                self.assertFalse(mirror.runnable(artifact))
                with mock.patch.object(mirror, 'run', return_value=json.dumps(artifact).encode()), \
                        self.assertRaisesRegex(mirror.MirrorError, 'not a container image'):
                    mirror.add_image(self.root / 'images.txt', 'docker.io/bitnami/redis:sha256-' + 'c'*64 + '.sig',
                                     'mirror/bitnami/redis')
        self.assertFalse((self.root / 'images.txt').exists())

    def test_add_accepts_an_index_whose_children_omit_the_optional_platform(self):
        path = self.root / 'images.txt'
        image = {'schemaVersion': 2, 'config': {'mediaType': 'application/vnd.oci.image.config.v1+json'},
                 'layers': [{'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'}]}
        child = json.dumps(image).encode()
        nested = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + hashlib.sha256(child).hexdigest()}]})
        index = json.dumps({'schemaVersion': 2, 'manifests': [
            {'mediaType': 'application/vnd.oci.image.manifest.v1+json',
             'digest': 'sha256:' + hashlib.sha256(child).hexdigest(), 'size': len(child)}]})
        # A nested index says nothing about its children, so each is fetched and checked by digest.
        outer = json.dumps({'schemaVersion': 2, 'manifests': [{
            'mediaType': 'application/vnd.oci.image.index.v1+json',
            'digest': 'sha256:' + hashlib.sha256(nested.encode()).hexdigest()}]})
        for raws, tag in (([index.encode(), child], '1.0'), ([outer.encode(), nested.encode(), child], '2.0')):
            with self.subTest(tag=tag), mock.patch.object(mirror, 'run', side_effect=raws) as call:
                self.assertEqual(mirror.main(['--catalog', str(path), 'add', f'example.org/team/image:{tag}',
                                              'mirror/image']), 0)
            self.assertEqual(call.call_count, len(raws))
        self.assertEqual(len(mirror.catalog(path)), 2)
        with mock.patch.object(mirror, 'run', side_effect=[outer.encode(), b'{"tampered": true}']), \
                self.assertRaisesRegex(mirror.MirrorError, 'manifest digest mismatch'):
            mirror.add_image(path, 'example.org/team/image:3.0', 'mirror/image')

    def test_add_accepts_every_layer_type_skopeo_copies(self):
        path = self.root / 'images.txt'
        layers = ('application/vnd.docker.image.rootfs.diff.tar', 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                  'application/vnd.docker.image.rootfs.foreign.diff.tar',
                  'application/vnd.docker.image.rootfs.foreign.diff.tar.gzip',
                  'application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+zstd',
                  'application/vnd.oci.image.layer.nondistributable.v1.tar+gzip')
        for number, layer in enumerate(layers):
            manifest = json.dumps({'schemaVersion': 2, 'mediaType': mirror.MANIFESTS[1],
                                   'config': {'mediaType': 'application/vnd.docker.container.image.v1+json'},
                                   'layers': [{'mediaType': layer}]}).encode()
            with self.subTest(layer=layer), mock.patch.object(mirror, 'run', return_value=manifest):
                mirror.add_image(path, f'example.org/team/image:{number}', 'mirror/image')
        self.assertEqual(len(mirror.catalog(path)), len(layers))

    def test_add_checks_each_index_child_and_refuses_an_index_of_signatures(self):
        path = self.root / 'images.txt'
        image_config = {'mediaType': 'application/vnd.oci.image.config.v1+json', 'size': 2,
                        'digest': 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'}
        # A cosign signature uses the image manifest media type, so its descriptor alone cannot tell.
        signature = json.dumps({'schemaVersion': 2, 'mediaType': mirror.MANIFESTS[0], 'config': image_config,
                                'layers': [{'mediaType': 'application/vnd.dev.cosign.simplesigning.v1+json'}]}).encode()
        image = json.dumps({'schemaVersion': 2, 'mediaType': mirror.MANIFESTS[0], 'config': image_config,
                            'layers': [{'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'}]}).encode()
        manifests = {'sha256:' + hashlib.sha256(raw).hexdigest(): raw for raw in (signature, image)}

        def listing(*children):
            return json.dumps({'schemaVersion': 2, 'mediaType': mirror.INDEXES[0], 'manifests': [
                {'mediaType': mirror.MANIFESTS[0], 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                 'size': len(raw)} for raw in children]}).encode()

        def registry(index):
            return lambda *args: manifests.get(args[-1].rsplit('@', 1)[-1], index)

        for index in (listing(signature), listing(signature, signature)):
            with self.subTest(index=index), mock.patch.object(mirror, 'run', side_effect=registry(index)), \
                    self.assertRaisesRegex(mirror.MirrorError, 'not a container image'):
                mirror.add_image(path, 'example.org/team/image:signatures', 'mirror/image')
        self.assertFalse(path.exists())
        with mock.patch.object(mirror, 'run', side_effect=registry(listing(signature, image))):
            mirror.add_image(path, 'example.org/team/image:1.0', 'mirror/image')
        self.assertEqual([i['tag'] for i in mirror.catalog(path)], ['1.0'])

    def test_skopeo_transport_omits_tag_but_keeps_port_and_digest(self):
        reference = 'registry.example.internal:5000/team/image:1.0@sha256:' + 'a'*64
        self.assertEqual(mirror.transport_reference(reference),
                         'registry.example.internal:5000/team/image@sha256:' + 'a'*64)


    def test_promote_copies_recorded_digests_from_dev_to_prod_and_verifies_both(self):
        os.environ.update({'SOURCE_REGISTRY': 'dev.example.internal', 'TARGET_REGISTRY': 'prod.example.internal',
                           'SOURCE_REGISTRY_TLS_VERIFY': 'false'})
        old, new, other = ('sha256:' + c * 64 for c in 'abc')
        record = self.root / 'imported.json'
        record.write_text(json.dumps([{'target': 'team/app', 'tag': '1.0', 'digest': old},
                                      {'target': 'team/app', 'tag': '1.0', 'digest': new},
                                      {'target': 'team/db', 'tag': '2', 'digest': other}]))
        digests = {'docker://dev.example.internal/team/app@' + new: new, 'docker://prod.example.internal/team/app:1.0': new,
                   'docker://dev.example.internal/team/db@' + other: other, 'docker://prod.example.internal/team/db:2': other}
        with mock.patch.object(mirror, 'copy') as copied, mock.patch('builtins.print'), \
                mock.patch.object(mirror, 'raw_digest', side_effect=lambda ref, side: digests[ref]):
            mirror.promote(record)
        self.assertEqual([c.args for c in copied.call_args_list], [
            ('docker://dev.example.internal/team/app@' + new, 'docker://prod.example.internal/team/app:1.0',
             'SOURCE_REGISTRY', 'TARGET_REGISTRY'),
            ('docker://dev.example.internal/team/db@' + other, 'docker://prod.example.internal/team/db:2',
             'SOURCE_REGISTRY', 'TARGET_REGISTRY')])
        digests['docker://prod.example.internal/team/db:2'] = old
        with mock.patch.object(mirror, 'copy'), mock.patch('builtins.print'), \
                mock.patch.object(mirror, 'raw_digest', side_effect=lambda ref, side: digests[ref]), \
                self.assertRaisesRegex(mirror.MirrorError, 'digest mismatch in TARGET_REGISTRY after the copy'):
            mirror.promote(record)
        record.write_text(json.dumps([{'target': 'team/app', 'tag': '1.0', 'digest': 'latest'}]))
        with self.assertRaisesRegex(mirror.MirrorError, 'invalid entry'):
            mirror.promote(record)
        os.environ['TARGET_REGISTRY'] = 'dev.example.internal'
        with self.assertRaisesRegex(mirror.MirrorError, 'same registry'):
            mirror.promote(record)

    def test_platform_selection_picks_one_child_or_fails(self):
        child = {'digest': 'sha256:' + 'b'*64, 'platform': {'os': 'linux', 'architecture': 'amd64'}}
        arm = {'digest': 'sha256:' + 'c'*64, 'platform': {'os': 'linux', 'architecture': 'arm', 'variant': 'v7'}}
        attestation = {'digest': 'sha256:' + 'd'*64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}}
        listing = json.dumps({'schemaVersion': 2, 'manifests': [child, arm, attestation]}).encode()
        pinned = 'sha256:' + 'a'*64
        with mock.patch.object(mirror, 'run', return_value=listing):
            self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY'), pinned)
            for wanted, expected in (('linux/amd64', child['digest']), ('linux/arm/v7', arm['digest'])):
                with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': wanted}):
                    self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY'), expected)
            with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/s390x'}), \
                    self.assertRaisesRegex(mirror.MirrorError, 'found 0'):
                mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY')
        single = b'{"schemaVersion":2,"config":{},"layers":[]}'
        with mock.patch.object(mirror, 'run', side_effect=[single, b'{"os": "linux", "architecture": "amd64"}']), \
                mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
            self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY'), pinned)
        # A single image is checked against its config: the wrong architecture or variant fails.
        for wanted, config in (('linux/amd64', {'os': 'linux', 'architecture': 'arm64'}),
                               ('linux/arm/v7', {'os': 'linux', 'architecture': 'arm', 'variant': 'v6'}),
                               ('linux/amd64', [])):
            with self.subTest(wanted=wanted, config=config), \
                    mock.patch.object(mirror, 'run', side_effect=[single, json.dumps(config).encode()]) as call, \
                    mock.patch.dict(os.environ, {'MIRROR_PLATFORM': wanted}), \
                    self.assertRaisesRegex(mirror.MirrorError, 'image, not ' + wanted):
                mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY')
            self.assertIn('--config', call.call_args.args)

    def test_index_without_platforms_is_selected_by_each_child_config(self):
        amd, arm = 'sha256:' + 'b'*64, 'sha256:' + 'c'*64
        configs = {amd: {'os': 'linux', 'architecture': 'amd64'}, arm: {'os': 'linux', 'architecture': 'arm64'}}
        signature = {'digest': 'sha256:' + 'd'*64, 'artifactType': 'application/vnd.dev.cosign.artifact.sig.v1+json'}
        pinned = 'sha256:' + 'a'*64

        def registry(children):
            listing = json.dumps({'schemaVersion': 2, 'manifests': children}).encode()

            def fake_run(*args):
                if '--config' not in args:
                    return listing
                return json.dumps(configs[args[-1].rsplit('@', 1)[1]]).encode()
            return fake_run

        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
            # An index child's platform is optional; the child's own config names it instead.
            for children, expected in (([{'digest': arm}, {'digest': amd}, signature], amd),
                                       ([{'digest': arm}, {'digest': amd, 'platform': configs[amd]}], amd)):
                with self.subTest(children=children), mock.patch.object(mirror, 'run', side_effect=registry(children)):
                    self.assertEqual(mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY'), expected)
            # Never the whole index: an arm64-only index fails, and so does a nested index nobody can select from.
            for children, found in (([{'digest': arm}], 0), ([{'digest': amd}, {'digest': amd}], 2),
                                    ([{'digest': amd, 'mediaType': mirror.INDEXES[0]}], 0)):
                with self.subTest(children=children), mock.patch.object(mirror, 'run', side_effect=registry(children)), \
                        self.assertRaisesRegex(mirror.MirrorError, f'found {found}'):
                    mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY')

    def test_referrer_or_nested_index_is_never_the_platform_image(self):
        amd, pinned = 'sha256:' + 'b'*64, 'sha256:' + 'a'*64
        wanted = {'os': 'linux', 'architecture': 'amd64'}
        image = {'mediaType': mirror.MANIFESTS[0], 'digest': amd, 'platform': wanted}
        # Each names linux/amd64 in its descriptor, but none is an image the index can select.
        impostors = {
            'signature': {'mediaType': mirror.MANIFESTS[0], 'digest': 'sha256:' + 'd'*64, 'platform': wanted,
                          'artifactType': 'application/vnd.dev.cosign.artifact.sig.v1+json'},
            'attestation': {'mediaType': mirror.MANIFESTS[0], 'digest': 'sha256:' + 'e'*64, 'platform': wanted,
                            'annotations': {'vnd.docker.reference.type': 'attestation-manifest'}},
            'nested index': {'mediaType': mirror.INDEXES[0], 'digest': 'sha256:' + 'f'*64, 'platform': wanted},
        }
        path = self.root / 'images.txt'
        path.write_text(f'docker.io/library/example:1.0@{pinned} mirror/example\n')
        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
            for name, impostor in impostors.items():
                for children, found in (([impostor], 0), ([impostor, image], 1)):
                    listing = json.dumps({'schemaVersion': 2, 'manifests': children}).encode()
                    with self.subTest(name, children=len(children)), mock.patch.object(mirror, 'run', return_value=listing):
                        if found:
                            self.assertEqual(mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY'), amd)
                            continue
                        with self.assertRaisesRegex(mirror.MirrorError, 'found 0'):
                            mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY')
                        with mock.patch.object(mirror, 'copy') as transport, \
                                mock.patch.object(mirror, 'raw_digest', return_value=impostor['digest']):
                            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
                        transport.assert_not_called()
                        self.assertFalse(self.ledger.exists())

    def test_index_of_another_platform_without_platforms_is_not_mirrored(self):
        building = self.stage / 'building'
        child, _ = image_manifest(building, '{}.manifest.json')
        root = blob(building, {'schemaVersion': 2, 'manifests': [child]})
        index = (building / root['digest'][7:]).read_bytes()
        path = self.root / 'images.txt'
        path.write_text('docker.io/arm64v8/alpine:3.20@' + root['digest'] + ' mirror/alpine\n')
        raws = {False: index, True: b'{"os": "linux", "architecture": "arm64"}'}
        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}), \
                mock.patch.object(mirror, 'run', side_effect=lambda *args: raws['--config' in args]), \
                mock.patch.object(mirror, 'copy') as transport, \
                mock.patch.object(mirror, 'raw_digest', return_value=root['digest']):
            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
        transport.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_single_image_of_another_platform_is_not_mirrored(self):
        building = self.stage / 'building'
        config = blob(building, {'architecture': 'arm64', 'os': 'linux'})
        root = blob(building, {'schemaVersion': 2, 'config': config, 'layers': []})
        directory = place(self.stage, building, root)
        path = self.root / 'images.txt'
        path.write_text('docker.io/arm64v8/alpine:3.20@' + root['digest'] + ' mirror/alpine\n')
        raws = {False: (directory / 'manifest.json').read_bytes(), True: (directory / config['digest'][7:]).read_bytes()}
        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}), \
                mock.patch.object(mirror, 'run', side_effect=lambda *args: raws['--config' in args]), \
                mock.patch.object(mirror, 'copy') as transport, \
                mock.patch.object(mirror, 'raw_digest', return_value=root['digest']):
            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
        transport.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_latest_is_paired_with_its_version_tag(self):
        inspected = {'Labels': {'org.opencontainers.image.version': '8.10.2'}, 'Env': ['APP_VERSION=8.10.1']}
        with mock.patch.object(mirror, 'run', return_value=json.dumps(inspected).encode()) as call:
            self.assertEqual(mirror.app_version('docker://low/redis@sha256:' + 'a'*64, 'TARGET_REGISTRY'), '8.10.2')
            self.assertNotIn('--override-arch=amd64', call.call_args.args)
            with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
                mirror.app_version('docker://low/redis@sha256:' + 'a'*64, 'TARGET_REGISTRY')
            self.assertIn('--override-arch=amd64', call.call_args.args)
        with mock.patch.object(mirror, 'run', side_effect=mirror.MirrorError('inspect timed out')), \
                self.assertRaisesRegex(mirror.MirrorError, 'cannot read the version'):
            mirror.app_version('docker://low/redis@sha256:' + 'a'*64, 'TARGET_REGISTRY')
        # Without MIRROR_PLATFORM an index is read through one of its images, not the host's platform.
        index = {'schemaVersion': 2, 'manifests': [
            {'digest': 'sha256:' + 'd'*64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}},
            {'digest': 'sha256:' + 'c'*64, 'platform': {'os': 'linux', 'architecture': 'arm64'}}]}
        with mock.patch.object(mirror, 'run', side_effect=[json.dumps(index).encode(),
                                                          json.dumps(inspected).encode()]) as call:
            self.assertEqual(mirror.app_version('docker://up/redis@sha256:' + 'a'*64, 'SOURCE_REGISTRY'), '8.10.2')
        self.assertEqual(call.call_args.args[-1], 'docker://up/redis@sha256:' + 'c'*64)
        for data, expected in (({'Env': ['APP_VERSION=8.10.1']}, '8.10.1'), ({'Labels': {'x': 'y'}}, ''),
                               ({'Labels': {'org.opencontainers.image.version': 'not a tag'}}, '')):
            with mock.patch.object(mirror, 'run', return_value=json.dumps(data).encode()):
                self.assertEqual(mirror.app_version('docker://low/redis', 'TARGET_REGISTRY'), expected)
        stored = 'sha256:' + 'b'*64
        for version, expected in (('8.10.2', ['latest', '8.10.2']), ('', ['latest'])):
            with mock.patch.object(mirror, 'app_version', return_value=version):
                self.assertEqual(mirror.publish_tags('latest', stored, 'docker://up/redis', 'SOURCE_REGISTRY'), expected)
                self.assertEqual(mirror.publish_tags('3.20', stored, 'docker://up/redis', 'SOURCE_REGISTRY'), ['3.20'])

    def test_share_blobs_hardlinks_a_repeated_blob_and_keeps_it_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            layout, blob = Path(directory), 'a' * 64
            for image in ('000001', '000002'):
                (layout / image).mkdir()
                (layout / image / blob).write_bytes(b'layer')
            mirror.share_blobs(layout, {})
            self.assertTrue((layout / '000001' / blob).samefile(layout / '000002' / blob))
            self.assertEqual((layout / '000002' / blob).read_bytes(), b'layer')
            with mock.patch.object(mirror.os, 'link', side_effect=OSError('no hardlinks')):
                (layout / '000003').mkdir()
                (layout / '000003' / blob).write_bytes(b'layer')
                mirror.share_blobs(layout, {})
            self.assertEqual((layout / '000003' / blob).read_bytes(), b'layer')

    def test_login_skips_a_registry_without_credentials_and_keeps_the_password_off_argv(self):
        os.environ.update({'TARGET_REGISTRY_USERNAME': 'robot', 'TARGET_REGISTRY_PASSWORD': 'pw',
                           'TARGET_REGISTRY_TLS_VERIFY': 'false'})
        with mock.patch.object(mirror.subprocess, 'run', return_value=mock.Mock(returncode=0)) as ran, \
                mock.patch('builtins.print'):
            mirror.login()
        ran.assert_called_once()  # no SOURCE_REGISTRY_USERNAME: no docker.io login
        args, kwargs = ran.call_args
        self.assertEqual(args[0], ['skopeo', 'login', '--tls-verify=false', '--username', 'robot',
                                   '--password-stdin', 'low.example.internal'])
        self.assertEqual(kwargs['input'], b'pw')

    def test_a_source_in_the_target_registry_reads_with_the_target_settings(self):
        self.assertEqual(mirror.side_of('low.example.internal/team/runner:1.2@sha256:' + 'a'*64), 'TARGET_REGISTRY')
        self.assertEqual(mirror.side_of('docker://low.example.internal/team/runner@sha256:' + 'a'*64), 'TARGET_REGISTRY')
        self.assertEqual(mirror.side_of('docker.io/library/alpine:3.20'), None)
        os.environ['TARGET_REGISTRY_TLS_VERIFY'] = 'false'
        self.assertEqual(mirror.options(mirror.side_of('low.example.internal/team/runner:1.2')), ['--tls-verify=false'])
        self.assertEqual(mirror.options(mirror.side_of('docker.io/library/alpine:3.20')), [])
        os.environ.update({'SOURCE_REGISTRY': 'mirror.example.internal:5000', 'SOURCE_REGISTRY_TLS_VERIFY': 'false'})
        self.assertEqual(mirror.options(mirror.side_of('mirror.example.internal:5000/a/b:1')), ['--tls-verify=false'])
        self.assertIsNone(mirror.side_of('quay.io/org/image:1'))
        self.assertEqual(mirror.options(mirror.side_of('quay.io/org/image:1')), [])  # TLS stays on for other hosts

    def test_login_without_a_username_logs_in_nowhere(self):
        with mock.patch.object(mirror.subprocess, 'run') as ran:
            mirror.login()  # a high registry without authentication
        ran.assert_not_called()


    def test_helm_charts_are_catalog_entries_without_a_platform(self):
        chart = {'schemaVersion': 2, 'config': {'mediaType': mirror.CHART_CONFIG},
                 'layers': [{'mediaType': mirror.CHART_LAYERS[0]}]}
        self.assertTrue(mirror.runnable(chart))
        self.assertFalse(mirror.runnable(chart | {'layers': [{'mediaType': 'text/plain'}]}))
        self.assertFalse(mirror.runnable(chart | {'layers': [{'mediaType': mirror.CHART_LAYERS[1]}]}))
        os.environ['MIRROR_PLATFORM'] = 'linux/amd64'
        with mock.patch.object(mirror, 'run', return_value=json.dumps(chart).encode()) as call:
            self.assertEqual(mirror.platform_digest('docker://up/chart@sha256:' + 'd'*64, 'sha256:' + 'd'*64, 'SOURCE_REGISTRY'),
                             'sha256:' + 'd'*64)
        self.assertEqual(call.call_count, 1)

    def test_covers_finds_images_the_mirror_lacks(self):
        digest = 'sha256:' + 'e'*64
        path = self.root / 'images.txt'
        path.write_text(f'docker.io/library/alpine:3.20@{digest} team-dev/library/alpine\n')
        containerfile = self.root / 'Containerfile'
        containerfile.write_text('ARG BASE=x\nFROM alpine:3.20 AS build\nFROM build\nFROM scratch\nFROM nginx AS nginx\n'
                                 'FROM --platform=linux/amd64 quay.example.internal/team-dev/library/alpine:3.20\n'
                                 'FROM ${BASE}\nFROM docker.io/library/busybox@' + digest + '\n')
        compose = self.root / 'compose.yaml'
        compose.write_text('services:\n  app:\n    image: "nginx:1.29"\n')
        with mock.patch('builtins.print') as said:
            with self.assertRaisesRegex(mirror.MirrorError, '1 image'):
                mirror.covers(path, [str(containerfile)])  # a stage named like its image is still checked
            with self.assertRaisesRegex(mirror.MirrorError, '2 image'):
                mirror.covers(path, [str(containerfile), str(compose)])
        self.assertIn(mock.call(f'missing: nginx:1.29 ({compose}:3)'), said.call_args_list)

    def test_add_list_expands_short_names_assumes_latest_skips_known_and_keeps_going(self):
        path = self.root / 'images.txt'
        path.write_text('# reviewed catalog\n\n'
                        'quay.io/prometheus/node-exporter:v1.9.1@sha256:' + 'e' * 64 + ' team/prometheus/node-exporter\n')
        lists = self.root / 'lists'
        lists.mkdir()
        (lists / 'b.txt').write_text('# monitoring\nprom/prometheus:v3.13.4\nquay.io/prometheus/node-exporter:v1.9.1\n')
        (lists / 'a.txt').write_text('alpine\nghcr.io/broken/Name:1\n')

        def resolve(source, target):
            if 'broken' in source:
                raise mirror.MirrorError('add needs registry/repo:tag and org/repo[/path]')
            name = source.split('@')[0]
            return mirror.parse_image(name + '@sha256:' + 'f' * 64, target), [name.rsplit(':', 1)[1]]

        with mock.patch.object(mirror, 'resolve_entry', side_effect=resolve), mock.patch('builtins.print') as said, \
                self.assertRaisesRegex(mirror.MirrorError, r'1 line\(s\) not added:\n  a.txt:2 ghcr.io/broken/Name:1'):
            mirror.add_list(path, lists, prefix='team')
        printed = [c.args[0] for c in said.call_args_list]
        self.assertIn('expanded: alpine -> docker.io/library/alpine:latest', printed)
        self.assertIn('expanded: prom/prometheus:v3.13.4 -> docker.io/prom/prometheus:v3.13.4', printed)
        self.assertIn('already in the catalog: b.txt:3 quay.io/prometheus/node-exporter:v1.9.1 -> '
                      'team/prometheus/node-exporter:v1.9.1', printed)
        self.assertEqual(path.read_text().splitlines(), [
            '# reviewed catalog', '',
            'docker.io/library/alpine:latest@sha256:' + 'f' * 64 + ' team/library/alpine',
            'docker.io/prom/prometheus:v3.13.4@sha256:' + 'f' * 64 + ' team/prom/prometheus',
            'quay.io/prometheus/node-exporter:v1.9.1@sha256:' + 'e' * 64 + ' team/prometheus/node-exporter'])
        self.assertEqual(len(mirror.catalog(path)), 3)

    def test_write_catalog_moves_an_entrys_comment_with_it(self):
        path = self.root / 'images.txt'
        path.write_text('# header\nquay.io/z/z:1@sha256:' + 'a' * 64 + ' t/z\n# why b is pinned\n'
                        'docker.io/b/b:1@sha256:' + 'b' * 64 + ' t/b\n')
        mirror.write_catalog(path, ['ghcr.io/g/g:1@sha256:' + 'c' * 64 + ' t/g'])
        self.assertEqual([line.split('@')[0] for line in path.read_text().splitlines()],
                         ['# header', '# why b is pinned', 'docker.io/b/b:1', 'ghcr.io/g/g:1', 'quay.io/z/z:1'])



if __name__ == "__main__":
    unittest.main()
