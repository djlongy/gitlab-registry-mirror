"""The command line: every command and option parses and behaves, streams and exit codes stay apart."""
import io
import json
import os
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror
import test_mirror
from test_mirror import LOW, MirrorCase, Registry


class CliTest(MirrorCase):
    def test_progress_is_on_stderr_and_data_on_stdout(self):
        with Registry(self) as registry:
            app, db = self.two_images(registry)
            self.assertEqual(self.cli('pending'), 0)
            self.assertEqual(self.out.getvalue().splitlines(), [f'docker.io/team/app:1.0@{app}', f'docker.io/team/db:2@{db}'])
            self.out.truncate(0)
            self.out.seek(0)
            self.assertEqual(self.cli('sync'), 0)
            self.assertEqual(self.out.getvalue(), '', 'sync prints progress, not data')
            self.assertIn('synced 2 image(s)', self.err.getvalue())

    def test_quiet_silences_progress_but_not_errors_or_data(self):
        with Registry(self) as registry:
            app, _ = self.two_images(registry)
            self.assertEqual(self.cli('--quiet', 'sync'), 0)
            self.assertEqual(self.err.getvalue(), '')
            self.assertEqual(self.cli('-q', 'pending'), 0)
            self.assertEqual(self.out.getvalue(), '')
            self.catalog.write_text('')
            self.assertEqual(self.cli('-q', 'sync'), 1)
            self.assertIn(f'{mirror.prog()}: catalog is empty', self.err.getvalue())

    def test_runtime_errors_and_warnings_carry_the_program_name(self):
        self.assertEqual(self.cli('sync'), 1)
        self.assertRegex(self.err.getvalue(), rf'^{mirror.prog()}: ')
        self.assertNotIn('error:', self.err.getvalue())
        self.err.truncate(0)
        self.err.seek(0)
        mirror.warn('careful')
        self.assertEqual(self.err.getvalue(), f'{mirror.prog()}: warning: careful\n')
        self.assertEqual(mirror.prog(), Path(sys.argv[0]).name)

    def run_cli(self, *args):
        """main() with SystemExit turned into a return code, like a shell sees it."""
        try:
            return self.cli(*args)
        except SystemExit as stop:
            return stop.code

    def test_version_prints_the_program_and_its_version_on_stdout(self):
        self.assertEqual(self.run_cli('--version'), 0)
        self.assertEqual(self.out.getvalue(), f'{mirror.prog()} {mirror.VERSION}\n')
        self.assertRegex(mirror.VERSION, r'^\d+\.\d+\.\d+$')

    def test_help_names_the_program_as_started_and_never_a_fixed_script_name(self):
        with mock.patch.object(sys, 'argv', ['registry-mirror']):
            self.assertEqual(self.run_cli('-h'), 0)
            self.assertIn('registry-mirror sync', self.out.getvalue())
            self.assertNotIn('mirror.py', self.out.getvalue())
            for command in ('sync', 'request', 'export', 'import', 'login', 'add'):
                self.out.truncate(0)
                self.out.seek(0)
                self.assertEqual(self.run_cli(command, '-h'), 0)
                self.assertNotIn('mirror.py', self.out.getvalue(), command)
                self.assertNotIn('{prog}', self.out.getvalue(), command)

    def test_exit_codes_are_0_for_success_1_for_a_runtime_failure_and_2_for_a_usage_error(self):
        self.assertEqual(self.run_cli('pending'), 1, 'no catalog: a runtime failure')
        self.assertEqual(self.run_cli('import'), 2, 'import with no source is a usage error')
        self.assertIn('import needs a tar or a directory', self.err.getvalue())
        self.assertIn('error:', self.err.getvalue(), 'argparse keeps its own usage-error prefix')
        self.assertEqual(self.run_cli('no-such-command'), 2)
        self.assertEqual(self.run_cli('sync', '--no-such-option'), 2)
        self.catalog.write_text('')
        self.assertEqual(self.run_cli('pending'), 0)

    def test_size_takes_coreutils_suffixes_and_a_bad_size_is_a_usage_error_before_any_work(self):
        for text, expected in (('5', 5), ('2K', 2048), ('2KiB', 2048), ('2KB', 2000), ('3M', 3 * 1024 ** 2),
                               ('3MB', 3 * 10 ** 6), ('1GiB', 1024 ** 3), ('1GB', 10 ** 9), ('1T', 1024 ** 4),
                               ('1TB', 10 ** 12), ('4 gib', 4 * 1024 ** 3)):
            self.assertEqual(mirror.size(text), expected, text)
        for bad in ('0', '', 'x', '1.5G', '4GBB', '-1', '4Gi'):
            with self.assertRaises(mirror.argparse.ArgumentTypeError):
                mirror.size(bad)
        with mock.patch.object(mirror, 'run', side_effect=AssertionError('no skopeo call before parsing')):
            self.assertEqual(self.run_cli('export', 'team/a:1', '--max-size', '4XB'), 2)
            self.assertIn('--max-size', self.err.getvalue())
            with mock.patch.dict(os.environ, {'MIRROR_EXPORT_MAX_SIZE': 'bogus'}):
                self.assertEqual(self.run_cli('export', 'team/a:1'), 2, 'the environment default is checked too')

    def test_boolean_environment_variables_accept_the_usual_words_in_any_case(self):
        for word in ('1', 'yes', 'y', 'true', 't', 'on', 'TRUE', 'Yes', ' On '):
            with mock.patch.dict(os.environ, {'FLAG': word}):
                self.assertTrue(mirror.env_flag('FLAG'), word)
                self.assertTrue(mirror.env_flag('FLAG', True), word)
        for word in ('0', 'no', 'n', 'false', 'f', 'off', 'FALSE', 'Off'):
            with mock.patch.dict(os.environ, {'FLAG': word}):
                self.assertFalse(mirror.env_flag('FLAG'), word)
                self.assertFalse(mirror.env_flag('FLAG', True), word)
        self.assertTrue(mirror.env_flag('FLAG', True))
        self.assertFalse(mirror.env_flag('FLAG'))
        with mock.patch.dict(os.environ, {'FLAG': ''}):
            self.assertTrue(mirror.env_flag('FLAG', True), 'empty is unset')
        with mock.patch.dict(os.environ, {'FLAG': 'maybe'}), self.assertRaises(mirror.UsageError):
            mirror.env_flag('FLAG')

    def test_an_unknown_boolean_is_a_usage_error_at_every_site(self):
        cases = (('MIRROR_VERBOSE', ('pending',), {}),
                 ('MIRROR_RESEND_ALL', ('export', '-o', self.root / 'a.tar'), {}),
                 ('MIRROR_REGISTRY_TLS_VERIFY', ('login',), {'MIRROR_REGISTRY_USERNAME': 'robot'}))
        self.catalog.write_text('')
        for name, args, extra in cases:
            self.err.truncate(0)
            self.err.seek(0)
            with mock.patch.dict(os.environ, {name: 'maybe', **extra}):
                self.assertEqual(self.run_cli(*args), 2, name)
                self.assertIn(f"{name}='maybe'", self.err.getvalue())

    def test_registry_and_source_registry_override_the_environment_and_are_required_when_neither_is_set(self):
        with Registry(self) as registry:
            app, _ = self.two_images(registry)
            self.assertEqual(self.cli('--registry', 'other.example.internal', 'sync'), 0)
            self.assertEqual(registry.tags['other.example.internal/team/app:1.0'], app)
            self.assertNotIn(f'{LOW}/team/app:1.0', registry.tags, '--registry beats MIRROR_REGISTRY')
            self.assertEqual(os.environ['MIRROR_REGISTRY'], 'other.example.internal')
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(self.run_cli('sync'), 1)
            self.assertIn('--registry or MIRROR_REGISTRY is required', self.err.getvalue())
        with mock.patch.dict(os.environ, clear=True):
            self.catalog.write_text('')
            self.assertEqual(self.cli('--source-registry', 'docker.io', 'list'), 0)
            self.assertEqual(os.environ['MIRROR_SOURCE_REGISTRY'], 'docker.io', '--source-registry sets the variable login reads')

    def logged_in(self, *args, stdin=b'', **env):
        """Run login with skopeo mocked; returns (exit code, the skopeo command, the stdin it was given)."""
        reader = io.TextIOWrapper(io.BytesIO(stdin))
        with mock.patch.object(mirror.subprocess, 'run', return_value=mock.Mock(returncode=0)) as ran, \
                mock.patch.object(sys, 'stdin', reader), mock.patch.dict(os.environ, env):
            code = self.run_cli(*args)
        command, kwargs = (ran.call_args.args[0], ran.call_args.kwargs) if ran.called else (None, {})
        return code, command, kwargs.get('input')

    def test_login_takes_a_registry_operand_a_username_and_the_password_on_stdin(self):
        code, command, given = self.logged_in('login', 'docker.io', '-u', 'me', '--password-stdin', stdin=b'tok\n')
        self.assertEqual((code, given), (0, b'tok'))
        self.assertEqual(command[:2], ['skopeo', 'login'])
        self.assertEqual(command[-4:], ['--username', 'me', '--password-stdin', 'docker.io'])
        self.assertNotIn('tok', ' '.join(command), 'the password is never an argument')
        self.assertIn('logged in to docker.io as me', self.err.getvalue())

    def test_login_with_no_operand_uses_the_registry_option_and_that_registrys_variables(self):
        code, command, given = self.logged_in('--registry', 'reg.example.internal', 'login', '-u', 'me',
                                              '--password-stdin', stdin=b'tok', MIRROR_REGISTRY=LOW)
        self.assertEqual((code, command[-1], given), (0, 'reg.example.internal', b'tok'))
        code, command, given = self.logged_in('login', MIRROR_REGISTRY_USERNAME='robot', MIRROR_REGISTRY_PASSWORD='pw')
        self.assertEqual((code, command[-1], command[-3], given), (0, LOW, 'robot', b'pw'))
        code, command, given = self.logged_in('login', '-u', 'other', MIRROR_REGISTRY_PASSWORD='pw')
        self.assertEqual((code, command[-3], given), (0, 'other', b'pw'), '-u picks the account, the stored password stays')

    def test_login_with_no_flags_logs_in_to_each_registry_that_has_a_username(self):
        code, command, given = self.logged_in('login', MIRROR_REGISTRY_USERNAME='a', MIRROR_REGISTRY_PASSWORD='1')
        self.assertEqual((code, command[-1], given), (0, LOW, b'1'))
        code, command, given = self.logged_in('login')
        self.assertEqual((code, command), (0, None), 'no username: no login')

    def test_login_without_an_account_or_with_a_bad_registry_is_a_usage_error(self):
        self.assertEqual(self.logged_in('login', 'docker.io')[0], 2)
        self.assertIn('needs -u USER', self.err.getvalue())
        self.assertEqual(self.logged_in('login', 'https://docker.io', '-u', 'me')[0], 2)

    def test_login_failure_is_a_runtime_error(self):
        with mock.patch.object(mirror.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr=b'denied')):
            self.assertEqual(self.run_cli('login', 'docker.io', '-u', 'me'), 1)
        self.assertIn('skopeo login docker.io failed', self.err.getvalue())

    def with_stdin(self, text):
        return mock.patch.object(sys, 'stdin', io.StringIO(text))

    def test_files_from_names_images_in_files_and_stdin_and_repeats(self):
        with Registry(self) as registry:
            for name in ('app:1.0', 'db:2', 'web:3'):
                registry.tags[f'{LOW}/team/{name}'] = registry.image(name)
            first = self.root / 'first.txt'
            first.write_text('# carry these\nteam/app:1.0\n')
            second = self.root / 'second.txt'
            second.write_text('team/web:3  # trailing comment\n')
            tar = self.root / 'carry.tar'
            with self.with_stdin('team/db:2\n'):
                self.assertEqual(self.cli('export', '-T', first, '--files-from', second, '-T', '-', '-o', tar), 0)
            self.assertEqual([image[0] for image in self.tar_images(tar)],
                             [f'{LOW}/team/app:1.0', f'{LOW}/team/web:3', f'{LOW}/team/db:2'])
            other = self.root / 'other.tar'
            self.assertEqual(self.cli('export', 'team/db:2', '-T', first, '-o', other), 0, 'names and files together')
            self.assertEqual(len(self.tar_images(other)), 2)

    def resolver(self):
        def resolve(source, target):
            name = source.split('@')[0]
            return mirror.parse_image(name + '@sha256:' + 'f' * 64, target), [name.rsplit(':', 1)[1]]
        return mock.patch.object(mirror, 'resolve_entry', side_effect=resolve)

    def test_add_takes_one_image_or_files_from_with_a_prefix_and_never_both(self):
        with self.resolver():
            self.assertEqual(self.cli('add', 'docker.io/prom/prometheus:v3.13.4', 'team/prometheus'), 0)
            listing = self.root / 'list.txt'
            listing.write_text('alpine:3.20\n')
            with self.with_stdin('quay.io/org/tool:1\n'):
                self.assertEqual(self.cli('add', '-T', listing, '--files-from', '-', '--prefix', 'team'), 0)
        self.assertEqual([image['target'] + ':' + image['tag'] for image in mirror.catalog(self.catalog)],
                         ['team/library/alpine:3.20', 'team/prometheus:v3.13.4', 'team/org/tool:1'])
        for args in (('add',), ('add', 'docker.io/a/b:1'), ('add', 'docker.io/a/b:1', 'team/b', 'extra'),
                     ('add', 'docker.io/a/b:1', 'team/b', '-T', listing), ('add', 'docker.io/a/b:1', 'team/b', '--prefix', 'x')):
            with self.subTest(args=args), mock.patch.object(mirror, 'resolve_entry', side_effect=AssertionError('no work')):
                self.assertEqual(self.run_cli(*args), 2)

    def high_import(self, registry, *args):
        os.environ['MIRROR_REGISTRY'] = 'high.example.internal'
        return self.cli('import', *args)

    def test_strip_components_removes_n_segments_from_flag_or_environment_and_excludes_rewrite(self):
        self.assertEqual(mirror.rebase('a/b/c', 2), 'c')
        self.assertEqual(mirror.rebase('a/b/c', 1), 'b/c')
        with self.assertRaisesRegex(mirror.MirrorError, 'no path left'):
            mirror.rebase('a/b', 2)
        with Registry(self) as registry:
            tar, digest = self.exported(registry, source=f'{LOW}/team/sub/app:1.0')
            self.assertEqual(self.high_import(registry, tar, '--strip-components', '2'), 0)
            self.assertEqual(registry.tags['high.example.internal/app:1.0'], digest)
            with mock.patch.dict(os.environ, {'MIRROR_STRIP_COMPONENTS': '1'}):
                self.assertEqual(self.high_import(registry, tar), 0)
            self.assertEqual(registry.tags['high.example.internal/sub/app:1.0'], digest)
            self.assertEqual(self.run_cli('import', tar, '--strip-components', '1', '--rewrite-base-path', 'org'), 2)
            with mock.patch.dict(os.environ, {'MIRROR_REWRITE_BASE_PATH': 'org'}):
                self.assertEqual(self.run_cli('import', tar, '--strip-components', '1'), 2, 'flag and variable together')
            for bad in ('0', '-1', 'x', '1.5'):
                self.assertEqual(self.run_cli('import', tar, '--strip-components', bad), 2, bad)
            with mock.patch.dict(os.environ, {'MIRROR_STRIP_COMPONENTS': 'many'}):
                self.assertEqual(self.run_cli('import', tar), 2)
            self.assertEqual(self.cli('export', f'{LOW}/team/sub/app:1.0', '--strip-components', '1', '-o', self.root / 'x.tar'), 0)
            self.assertEqual(self.tar_images(self.root / 'x.tar')[0][1], 'sub/app')

    def test_no_clobber_refuses_the_whole_tar_before_any_push(self):
        with Registry(self) as registry:
            registry.tags[f'{LOW}/team/app:1.0'] = registry.image('app')
            registry.tags[f'{LOW}/team/db:2'] = registry.image('db')
            tar = self.root / 'two.tar'
            self.assertEqual(self.cli('export', 'team/app:1.0', 'team/db:2', '-o', tar), 0)
            registry.tags['high.example.internal/team/db:2'] = 'sha256:' + 'b' * 64
            self.assertEqual(self.high_import(registry, tar, '--no-clobber'), 1)
            self.assertIn('--no-clobber does not move a tag', self.err.getvalue())
            self.assertNotIn('high.example.internal/team/app:1.0', registry.tags, 'nothing was pushed')

    def test_keep_in_both_spellings_leaves_the_inbox_alone(self):
        with Registry(self) as registry:
            for spelling in ('-k', '--keep'):
                inbox = self.root / ('inbox' + spelling.strip('-'))
                inbox.mkdir()
                tar, _ = self.exported(registry, name=f'c{spelling.strip("-")}.tar')
                for path in (tar, tar.with_name(tar.name + '.sha256')):
                    os.replace(path, inbox / path.name)
                self.assertEqual(self.high_import(registry, inbox, spelling), 0)
                self.assertEqual(len(list(inbox.iterdir())), 2, spelling)
            fresh = self.root / 'fresh'
            fresh.mkdir()
            tar, _ = self.exported(registry, name='gone.tar')
            for path in (tar, tar.with_name(tar.name + '.sha256')):
                os.replace(path, fresh / path.name)
            self.assertEqual(self.high_import(registry, fresh), 0)
            self.assertEqual(list(fresh.iterdir()), [], 'without -k an inbox is emptied')
            self.assertIn('inbox', self.run_cli_help('import'))

    def run_cli_help(self, command):
        self.out.truncate(0)
        self.out.seek(0)
        self.run_cli(command, '-h')
        return self.out.getvalue()

    def test_list_prints_the_catalog_on_stdout_and_check_reads_files_or_stdin(self):
        digest = 'f' * 64
        self.catalog.write_text(f'docker.io/library/alpine:3.20@sha256:{digest} team/alpine\n')
        self.assertEqual(self.cli('list'), 0)
        self.assertEqual(self.out.getvalue(), 'docker.io/library/alpine:3.20@sha256:' + digest + ' -> team/alpine:3.20\n')
        self.assertEqual(self.err.getvalue(), '')
        with self.with_stdin('FROM alpine:3.20\n'):
            self.assertEqual(self.cli('check', '-'), 0)
        self.out.truncate(0)
        self.out.seek(0)
        with self.with_stdin('FROM busybox:1\n'):
            self.assertEqual(self.cli('check', '-'), 1)
        self.assertIn('missing: busybox:1 (-:1)', self.out.getvalue())
        containerfile = self.root / 'Containerfile'
        containerfile.write_text('FROM alpine:3.20\n')
        self.assertEqual(self.cli('check', containerfile), 0)

    def snapshot(self):
        """Every file under the temporary root with its bytes: a dry run must leave this exactly as it was."""
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob('*')) if p.is_file()}

    def clear(self, stream):
        stream.truncate(0)
        stream.seek(0)

    def test_dry_run_sync_prints_the_copies_and_saves_nothing(self):
        for spelling in ('-n', '--dry-run'):
            with self.subTest(spelling), Registry(self) as registry, mock.patch.object(mirror, 'post') as webhook:
                self.two_images(registry)
                os.environ['MIRROR_REQUEST_URL'] = 'https://hook.example.internal/in'
                before = self.snapshot()
                self.clear(self.out)
                self.assertEqual(self.cli('sync', spelling), 0)
                self.assertEqual(sorted(self.out.getvalue().splitlines()),
                                 sorted(f'would copy: docker.io/team/{n.split(":")[0]}@{registry.image(n.split(":")[0])} -> {LOW}/team/{n}'
                                        for n in ('app:1.0', 'db:2')))
                self.assertEqual(registry.tags, {}, 'nothing was copied')
                self.assertEqual(self.snapshot(), before, 'no ledger was saved')
                self.assertFalse(self.ledger.exists())
                webhook.assert_not_called()
                self.assertEqual(self.cli('sync'), 0)
                self.assertEqual(len(registry.tags), 2, 'the same run without the flag does copy')
                self.clear(self.out)
                saved = self.ledger.read_bytes()
                self.assertEqual(self.cli('sync', spelling), 0)
                self.assertEqual(self.out.getvalue(), '', 'nothing left to copy')
                self.assertEqual(self.ledger.read_bytes(), saved)
                self.ledger.unlink()
                self.clear(self.err)

    def test_dry_run_export_names_the_tars_and_writes_nothing(self):
        with Registry(self) as registry:
            self.two_images(registry)
            self.cli('sync')
            before = self.snapshot()
            for spelling in ('-n', '--dry-run'):
                self.clear(self.out)
                self.assertEqual(self.cli('export', '--all', spelling, '-o', self.root / 'carry.tar'), 0)
                lines = self.out.getvalue().splitlines()
                self.assertEqual(lines[0], 'would write: ' + str(self.root / 'carry.tar') + ' and carry.tar.sha256 (2 image tag(s))')
                self.assertEqual(len(lines), 3)
                self.assertTrue(lines[1].startswith(f'  {LOW}/team/app:1.0@sha256:'))
            self.assertEqual(self.snapshot(), before)
            self.assertFalse([p for p in self.root.iterdir() if p.name.startswith('.mirror-staging')])
            self.clear(self.out)
            with mock.patch.object(mirror, 'groups', side_effect=lambda images, cap: [images[:1], images[1:]]):
                self.assertEqual(self.cli('export', '--all', '-n', '-o', self.root / 'split.tar', '--max-size', '1'), 0)
            self.assertIn('split-001-of-002.tar', self.out.getvalue())
            self.assertIn('split-002-of-002.tar', self.out.getvalue())
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.cli('export', '--all', '-o', self.root / 'carry.tar'), 0, 'a real run still works afterwards')
            self.assertEqual(self.run_cli('export', '-o', self.root / 'bare.tar'), 2, 'export names its images or takes --all')
            self.assertIn('export needs images to copy', self.err.getvalue())

    def test_dry_run_import_prints_the_plan_and_pushes_and_removes_nothing(self):
        inbox = self.root / 'inbox'
        inbox.mkdir()
        with Registry(self) as registry:
            tar, digest = self.exported(registry)
            for path in (tar, tar.with_name('carry.tar.sha256')):
                os.replace(path, inbox / path.name)
            os.environ['MIRROR_REGISTRY'] = 'high.example.internal'
            before = self.snapshot()
            tags = dict(registry.tags)
            for spelling in ('-n', '--dry-run'):
                self.clear(self.out)
                self.assertEqual(self.cli('import', inbox, spelling), 0)
                self.assertEqual(self.out.getvalue().splitlines(), [
                    f'would push: high.example.internal/team/app:1.0@{digest}', f'would remove: {inbox}/carry.tar'])
            self.clear(self.out)
            self.assertEqual(self.cli('import', inbox, '-n', '-k'), 0)
            self.assertEqual(self.out.getvalue().splitlines(), [f'would push: high.example.internal/team/app:1.0@{digest}'],
                             '-k keeps the inbox, so nothing would be removed')
            self.clear(self.out)
            self.assertEqual(self.cli('import', inbox / 'carry.tar', '-n', '--rewrite-base-path', 'org'), 0)
            self.assertEqual(self.out.getvalue(), f'would push: high.example.internal/org/app:1.0@{digest}\n')
            self.assertEqual((registry.tags, self.snapshot()), (tags, before), 'no push, no removal')
            registry.tags['high.example.internal/team/app:1.0'] = 'sha256:' + 'b' * 64
            self.clear(self.out)
            self.assertEqual(self.cli('import', inbox / 'carry.tar', '-n'), 0)
            self.assertIn('would move tag: high.example.internal/team/app:1.0 from sha256:' + 'b' * 64, self.out.getvalue())
            self.assertEqual(self.cli('import', inbox / 'carry.tar', '-n', '--no-clobber'), 1, 'the real refusal shows in a dry run')
            registry.tags['high.example.internal/team/app:1.0'] = digest
            self.clear(self.out)
            self.assertEqual(self.cli('import', inbox / 'carry.tar', '-n'), 0)
            self.assertEqual(self.out.getvalue(), f'already there: high.example.internal/team/app:1.0@{digest}\n')
            self.assertEqual(self.cli('import', inbox), 0)
            self.assertEqual(list(inbox.iterdir()), [], 'the same run without the flag imports and empties the inbox')

    def test_export_to_dash_streams_a_tar_that_import_accepts(self):
        class Stdout(io.StringIO):
            tty = False

            def __init__(self):
                super().__init__()
                self.buffer = io.BytesIO()

            def isatty(self):
                return self.tty

        os.chdir(self.root)
        self.addCleanup(os.chdir, Path(__file__).resolve().parents[1])
        with Registry(self) as registry:
            self.two_images(registry)
            self.cli('sync')
            before = self.snapshot()
            stdout = Stdout()
            with mock.patch.object(sys, 'stdout', stdout):
                self.assertEqual(self.cli('export', '--all', '-o', '-'), 0)
            self.assertEqual(stdout.getvalue(), '', 'only the tar goes to stdout')
            self.assertIn('to stdout', self.err.getvalue())
            self.assertEqual(self.snapshot(), before, 'no tar, no .sha256 and no staging directory left on disk')
            carried = self.root / 'carried.tar'
            carried.write_bytes(stdout.buffer.getvalue())
            digest = mirror.sha256(carried)  # what `| tee carry.tar | sha256sum` would print
            self.assertEqual(digest, mirror.hashlib.sha256(stdout.buffer.getvalue()).hexdigest())
            self.assertEqual(sorted(i[2] for i in self.tar_images(carried)), ['1.0', '2'])
            os.environ['MIRROR_REGISTRY'] = 'high.example.internal'
            self.assertEqual(self.run_cli('import', carried), 1, 'no .sha256 came with the stream')
            self.assertEqual(self.cli('import', carried, '--no-checksum'), 0)
            self.assertEqual(sorted(t for t in registry.tags if t.startswith('high.example.internal/')),
                             ['high.example.internal/team/app:1.0', 'high.example.internal/team/db:2'])
            carried.with_name('carried.tar.sha256').write_text(f'{digest}  carried.tar\n')
            self.assertEqual(self.cli('import', carried), 0, 'with the checksum the reader computed, import checks it too')
            os.environ['MIRROR_REGISTRY'] = LOW
            for args, reason in ((('--max-size', '1GiB'), '--max-size'), (('--dry-run',), '--dry-run')):
                broken = Stdout()
                with mock.patch.object(sys, 'stdout', broken):
                    self.assertEqual(self.run_cli('export', '--all', '-o', '-', *args), 2, reason)
                self.assertEqual(broken.buffer.getvalue(), b'')
            terminal = Stdout()
            terminal.tty = True
            with mock.patch.object(sys, 'stdout', terminal):
                self.assertEqual(self.run_cli('export', '--all', '-o', '-'), 2)
            self.assertIn('refusing to write a tar to a terminal', self.err.getvalue())
            self.assertEqual(terminal.buffer.getvalue(), b'')

    def test_catalog_and_ledger_options_choose_the_files(self):
        other = self.root / 'other-catalog.txt'
        saved = self.root / 'elsewhere' / 'ledger.json'
        with Registry(self) as registry:
            digest = registry.image('app')
            other.write_text(f'docker.io/team/app:1.0@{digest} team/app\n')
            self.assertEqual(mirror.main(['--catalog', str(other), '--ledger', str(saved), 'sync']), 0)
        self.assertEqual(json.loads(saved.read_text())['synced']['team/app:1.0']['digest'], digest)
        self.assertFalse((self.root / '.mirror-ledger.json').exists(), '--ledger beats the file beside the catalog')
        self.assertFalse(self.catalog.exists(), 'the default catalog is never touched')
        self.assertEqual(mirror.main(['--catalog', str(other), '--ledger', str(saved), 'pending']), 0)
        self.assertEqual(self.out.getvalue(), '', 'the ledger given by --ledger says everything is synced')
        self.assertEqual(mirror.main(['--catalog', str(other), '--ledger', str(self.root / 'empty.json'), 'pending']), 0)
        self.assertEqual(self.out.getvalue().splitlines(), [f'docker.io/team/app:1.0@{digest}'])
        parser_default = mirror.ROOT / 'images.txt'
        with mock.patch.object(mirror, 'catalog', side_effect=mirror.MirrorError('stop')) as read:
            self.assertEqual(mirror.main(['list']), 1)
        read.assert_called_once_with(parser_default)

    def test_verbose_prints_each_command_on_stderr_and_quiet_by_default(self):
        listed = mock.Mock(returncode=0, stdout=b'{"Tags": []}', stderr=b'')
        with mock.patch.object(mirror.subprocess, 'run', return_value=listed):
            self.assertEqual(self.cli('export', 'team/app'), 0)
            self.assertNotIn('+ skopeo', self.err.getvalue())
            self.assertEqual(self.cli('-v', 'export', 'team/app'), 0)
            self.assertIn(f'+ skopeo list-tags docker://{LOW}/team/app', self.err.getvalue())
            self.assertEqual(self.out.getvalue(), '')
        with mock.patch.dict(os.environ, {'MIRROR_VERBOSE': 'yes'}), mock.patch.object(mirror.subprocess, 'run', return_value=listed):
            self.clear(self.err)
            self.assertEqual(self.cli('export', 'team/app'), 0)
            self.assertIn('+ skopeo', self.err.getvalue())
            self.assertEqual(self.cli('--verbose', '--quiet', 'export', 'team/app'), 0)

    def test_import_staging_dir_is_where_the_working_copy_goes_and_is_removed(self):
        with Registry(self) as registry:
            tar, _ = self.exported(registry)
            os.environ['MIRROR_REGISTRY'] = 'high.example.internal'
            staging = self.root / 'big-disk' / 'work'
            self.assertEqual(self.cli('import', tar, '--staging-dir', staging), 0)
            self.assertIn(str(staging), self.err.getvalue())
            self.assertEqual(list(staging.iterdir()), [], 'the working copy is gone')
            with mock.patch.dict(os.environ, {'MIRROR_STAGING_DIR': str(self.root / 'from-env')}):
                self.assertEqual(self.cli('import', tar), 0)
            self.assertTrue((self.root / 'from-env').is_dir())

    def test_the_package_ledger_is_reached_with_the_ledger_token_project_and_api_url_variables(self):
        seen = []

        def urlopen(request, **_):
            seen.append((request.full_url, request.headers.get('Private-token')))
            return io.BytesIO(json.dumps({'registry': LOW, 'synced': {}, 'sent': {}, 'platforms': {}}).encode())

        env = {'MIRROR_LEDGER': 'gitlab', 'MIRROR_LEDGER_TOKEN': 'tok', 'MIRROR_LEDGER_PROJECT': 'infra/mirror',
               'MIRROR_GITLAB_API_URL': 'https://git.example.internal/api/v4/'}
        self.catalog.write_text('')
        with mock.patch.dict(os.environ, env), mock.patch.object(mirror.urllib.request, 'urlopen', side_effect=urlopen):
            self.assertEqual(self.cli('pending'), 0)
        self.assertEqual(seen, [('https://git.example.internal/api/v4/projects/infra%2Fmirror/packages/generic/'
                                 'registry-mirror-ledger/v2/ledger.json', 'tok')])
        with mock.patch.dict(os.environ, {**env, 'MIRROR_LEDGER_TOKEN': ''}, clear=True):
            self.assertEqual(self.run_cli('pending'), 1)
        self.assertIn('MIRROR_LEDGER_TOKEN', self.err.getvalue())

    def test_the_registry_variables_set_the_registry_the_account_and_tls(self):
        with mock.patch.dict(os.environ, {'MIRROR_REGISTRY': 'plain.example.internal:5000',
                                          'MIRROR_REGISTRY_TLS_VERIFY': 'no', 'MIRROR_SOURCE_REGISTRY': 'src.example.internal',
                                          'MIRROR_SOURCE_REGISTRY_TLS_VERIFY': 'off'}):
            self.assertEqual(mirror.registry('MIRROR_REGISTRY'), 'plain.example.internal:5000')
            self.assertEqual(mirror.options('MIRROR_REGISTRY'), ['--tls-verify=false'])
            self.assertEqual(mirror.options('MIRROR_SOURCE_REGISTRY', 'src-'), ['--src-tls-verify=false'])
        code, command, given = self.logged_in('login', MIRROR_REGISTRY_USERNAME='a', MIRROR_REGISTRY_PASSWORD='b',
                                              MIRROR_SOURCE_REGISTRY='src.example.internal',
                                              MIRROR_SOURCE_REGISTRY_USERNAME='c', MIRROR_SOURCE_REGISTRY_PASSWORD='d')
        self.assertEqual(code, 0)
        self.assertEqual(given, b'd', 'both registries log in, the source last')


# Every command and option of the parser, with the test that fails when its name or behaviour breaks.
COVERAGE = {
    ' --catalog': 'test_catalog_and_ledger_options_choose_the_files',
    ' --ledger': 'test_catalog_and_ledger_options_choose_the_files',
    ' --registry': 'test_registry_and_source_registry_override_the_environment_and_are_required_when_neither_is_set',
    ' --source-registry': 'test_registry_and_source_registry_override_the_environment_and_are_required_when_neither_is_set',
    ' --verbose': 'test_verbose_prints_each_command_on_stderr_and_quiet_by_default',
    ' --quiet': 'test_quiet_silences_progress_but_not_errors_or_data',
    ' --version': 'test_version_prints_the_program_and_its_version_on_stdout',
    'login': 'test_login_takes_a_registry_operand_a_username_and_the_password_on_stdin',
    'login login_registry': 'test_login_takes_a_registry_operand_a_username_and_the_password_on_stdin',
    'login --username': 'test_login_takes_a_registry_operand_a_username_and_the_password_on_stdin',
    'login --password-stdin': 'test_login_takes_a_registry_operand_a_username_and_the_password_on_stdin',
    'add': 'test_add_takes_one_image_or_files_from_with_a_prefix_and_never_both',
    'add operands': 'test_add_takes_one_image_or_files_from_with_a_prefix_and_never_both',
    'add --files-from': 'test_add_takes_one_image_or_files_from_with_a_prefix_and_never_both',
    'add --prefix': 'test_add_takes_one_image_or_files_from_with_a_prefix_and_never_both',
    'list': 'test_list_prints_the_catalog_on_stdout_and_check_reads_files_or_stdin',
    'pending': 'test_pending_lists_what_sync_has_not_copied',
    'check': 'test_list_prints_the_catalog_on_stdout_and_check_reads_files_or_stdin',
    'check files': 'test_list_prints_the_catalog_on_stdout_and_check_reads_files_or_stdin',
    'sync': 'test_sync_copies_by_digest_records_the_ledger_and_is_idempotent',
    'sync --dry-run': 'test_dry_run_sync_prints_the_copies_and_saves_nothing',
    'export': 'test_export_takes_named_images_or_every_synced_one_with_all',
    'request': 'test_request_names_images_txt_lines_or_references_and_refuses_another_registry',
    'request --files-from': 'test_request_names_images_txt_lines_or_references_and_refuses_another_registry',
    'request --all': 'test_request_names_images_txt_lines_or_references_and_refuses_another_registry',
    'request --dry-run': 'test_request_names_images_txt_lines_or_references_and_refuses_another_registry',
    'request images': 'test_request_names_images_txt_lines_or_references_and_refuses_another_registry',
    'export images': 'test_export_names_images_from_args_a_list_or_any_registry_and_rewrites_the_base_path',
    'export --files-from': 'test_files_from_names_images_in_files_and_stdin_and_repeats',
    'export --all': 'test_export_takes_named_images_or_every_synced_one_with_all',
    'export --output': 'test_export_to_dash_streams_a_tar_that_import_accepts',
    'export --max-size': 'test_max_size_splits_whole_images_into_several_tars_and_none_means_one',
    'export --staging-dir': 'test_staging_is_beside_the_tar_or_overridden_and_always_removed',
    'export --rewrite-base-path': 'test_export_names_images_from_args_a_list_or_any_registry_and_rewrites_the_base_path',
    'export --strip-components': 'test_strip_components_removes_n_segments_from_flag_or_environment_and_excludes_rewrite',
    'export --dry-run': 'test_dry_run_export_names_the_tars_and_writes_nothing',
    'import': 'test_import_pushes_digests_unchanged_skips_present_and_moves_a_tag_unless_no_clobber',
    'import sources': 'test_a_directory_is_an_inbox_and_imported_tars_leave_it',
    'import --keep': 'test_keep_in_both_spellings_leaves_the_inbox_alone',
    'import --no-clobber': 'test_no_clobber_refuses_the_whole_tar_before_any_push',
    'import --no-checksum': 'test_import_no_checksum_takes_a_tar_without_its_sha256_but_still_checks_one_that_is_there',
    'import --staging-dir': 'test_import_staging_dir_is_where_the_working_copy_goes_and_is_removed',
    'import --rewrite-base-path': 'test_import_pushes_digests_unchanged_skips_present_and_moves_a_tag_unless_no_clobber',
    'import --strip-components': 'test_strip_components_removes_n_segments_from_flag_or_environment_and_excludes_rewrite',
    'import --dry-run': 'test_dry_run_import_prints_the_plan_and_pushes_and_removes_nothing',
}


def parser_items():
    """(key, action or None, help text) for every command and every option or operand of every command."""
    parser = mirror.build_parser()
    items = []
    for action in parser._actions:
        if isinstance(action, mirror.argparse._SubParsersAction):
            for choice in action._choices_actions:
                items.append((choice.dest, None, choice.help))
            for name, sub in action.choices.items():
                items += [(f'{name} {a.option_strings[-1] if a.option_strings else a.dest}', a, a.help)
                          for a in sub._actions if not isinstance(a, mirror.argparse._HelpAction)]
        elif not isinstance(action, mirror.argparse._HelpAction):
            items.append((f' {action.option_strings[-1]}', action, action.help))
    return items


class CoverageTest(unittest.TestCase):
    def test_every_command_and_option_has_a_help_string_and_a_test_that_names_it(self):
        items = parser_items()
        keys = {key for key, _, _ in items}
        self.assertEqual(sorted(keys - set(COVERAGE)), [], 'add a COVERAGE entry and a test for each new option')
        self.assertEqual(sorted(set(COVERAGE) - keys), [], 'remove the COVERAGE entry of an option that is gone')
        for key, action, text in items:
            with self.subTest(key):
                self.assertTrue((text or '').strip(), f'{key} has no help string')
                name = COVERAGE[key]
                method = getattr(CliTest, name, None) or getattr(test_mirror.MirrorTest, name, None)
                self.assertIsNotNone(method, f'{key}: no test named {name}')
                source = inspect.getsource(method)
                spellings = action.option_strings if action else [key]
                if action and not action.option_strings:
                    continue  # an operand: the test only has to exist
                self.assertTrue(any(f"'{spelling}'" in source or f'"{spelling}"' in source for spelling in spellings),
                                f'{name} never uses {" or ".join(spellings)}')
