"""Offline sweep tests; all identities and repositories are made up."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import pii_sweep as sweep


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sweep-test-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / 'repo'
        self.root.mkdir()
        self.cfg = self.home / 'config'
        self.cfg.mkdir()
        self.global_config = self.home / 'gitconfig'
        self.global_config.write_text('[user]\n name = Alice Example\n email = alice@example.com\n', encoding='utf-8')
        # A test run inside a git hook inherits GIT_DIR and friends: they would point git at the developer's repo.
        for name in [n for n in os.environ if n.startswith('GIT_')]:
            self.addCleanup(os.environ.__setitem__, name, os.environ.pop(name))
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'USERPROFILE': str(self.home),
                                               'ASK_CONFIG_DIR': str(self.cfg),
                                               'GIT_CONFIG_GLOBAL': str(self.global_config),
                                               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_COUNT': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git('init', '-q')
        self.git('config', 'user.name', 'publichandle')
        self.git('config', 'user.email', 'publichandle@users.noreply.github.com')
        self.git('remote', 'add', 'origin', 'https://github.com/publichandle/sample.git')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def add(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        self.git('add', '--', name)

    def run_main(self, args=(), terms=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sweep.main(list(args), root=self.root, terms=terms)
        return code, out.getvalue()

    def test_kinds_and_redaction(self):
        self.add('notes.txt', 'hello alice\nalice@corp.example\x2ecom.org\n/Users/alice/code\n')  # pii-sweep: ignore
        code, out = self.run_main(terms=['alice'])
        self.assertEqual(code, 1)
        self.assertIn('notes.txt:1: personal term', out)
        self.assertIn('notes.txt:2: email', out)
        self.assertIn('notes.txt:3: home path', out)
        self.assertNotIn('alice', out)

    def test_reserved_emails(self):
        addresses = ['alice@' + d for d in ('example.com', 'example.org', 'example.net', 'corp.example',
                                           'corp.invalid', 'corp.test', 'localhost', 'users.noreply.github.com')]
        self.add('notes.txt', '\n'.join(addresses))
        self.assertEqual(self.run_main()[0], 0)

    def test_home_placeholders(self):
        self.add('notes.txt', '\n'.join('/Users/' + name + '/src' for name in
                 ['someone', 'me', 'user', 'you', 'example', 'shared', 'public', 'runner', 'x', '<name>', '$USER']))
        self.assertEqual(self.run_main()[0], 0)

    def test_unicode_and_windows_homes(self):
        for text in ['/home/álîce/code', '/Users/Аліса/code', r'C:\Users\Alice\code']:  # pii-sweep: ignore
            self.assertEqual(sweep.scan_text('a', text.encode(), []), [('a', 1, 'home path')])

    def test_not_an_address_or_a_home(self):
        # No dot in the domain, and a home folder name inside another path: neither is personal data.
        for text in ('"person@exam"', '"someone@sub." + domain', 'movePath: /outside/home/new.txt'):
            with self.subTest(text=text):
                self.assertEqual(sweep.scan_text('a', text.encode(), []), [])
        self.assertEqual(sweep.scan_text('a', b'C:/Users/alice/x', []), [('a', 1, 'home path')])  # pii-sweep: ignore

    def test_public_origin_and_local_identity_only(self):
        self.add('notes.txt', 'https://GITHUB.COM/PUBLICHANDLE/SAMPLE.git\npublichandle\n'
                 'publichandle@users.noreply.github.com\nAlice Example\n')
        code, out = self.run_main(terms=['publichandle', 'alice example'])
        self.assertEqual(code, 1)
        self.assertEqual(out, 'notes.txt:4: personal term\n')
        # Remove local identity: the same handle outside the remote is now private.
        self.git('config', '--unset', 'user.name')
        self.git('config', '--unset', 'user.email')
        code, out = self.run_main(terms=['publichandle'])
        self.assertEqual(code, 1)
        self.assertNotIn(':1:', out)
        self.assertIn(':2: personal term', out)

    def test_public_exemptions_do_not_hide_other_repositories_or_longer_handles(self):
        ex = sweep.public_exemptions(self.root)
        for text in ['github.com/publichandle/sample-other', 'publichandle-private']:
            self.assertTrue(sweep.scan_text('a', text.encode(), ['publichandle'], ex))

    def test_path_terms(self):
        self.add('alice/notes.txt', 'safe')
        self.assertEqual(self.run_main(terms=['ALICE'])[0], 1)

    def test_staged_reads_blobs(self):
        self.add('notes.txt', 'alice')
        (self.root / 'notes.txt').write_text('safe', encoding='utf-8')
        self.assertEqual(self.run_main(terms=['alice'])[0], 0)
        self.assertEqual(self.run_main(['--staged'], ['alice'])[0], 1)
        (self.root / 'notes.txt').unlink()
        self.assertEqual(self.run_main(['--staged'], ['alice'])[0], 1)

    def test_binary_and_untracked_are_skipped(self):
        self.add('image.bin', b'\0alice@corp.example.com')
        (self.root / 'untracked').write_text('alice', encoding='utf-8')
        self.assertEqual(self.run_main(terms=['alice']), (0, 'pii sweep: clean (0 files)\n'))

    def test_machine_sources(self):
        (self.cfg / 'machine.json').write_text(json.dumps({'work_domains': ['corp.example']}), encoding='utf-8')
        (self.cfg / 'pii_deny.txt').write_text('# ignored\n\nSecretword\n', encoding='utf-8')
        guard = self.home / '.config/pii-guard'
        guard.mkdir(parents=True)
        (guard / 'patterns.txt').write_text('Hiddenword\n', encoding='utf-8')
        terms = sweep.machine_terms(self.home, self.cfg, 'alice', 'Alice_Macbook.local',
                                    ['Alice Example', 'alice@example.com'])
        self.assertTrue({'alice', 'alice-macbook-local', 'example', 'corp.example', 'secretword', 'hiddenword'} <= terms)
        self.assertFalse({'com', 'local', 'macbook', 'ignored'} & terms)

    def test_hostname_is_a_term_only_whole(self):
        terms = sweep.machine_terms(self.home, self.cfg, 'someone', 'Quiet_Hill', [])
        self.assertTrue({'quiet_hill', 'quiet-hill'} <= terms)
        self.assertFalse({'quiet', 'hill'} & terms)

    def test_main_uses_global_config_and_override_folder(self):
        self.add('notes.txt', 'alice')
        with mock.patch.object(sweep.getpass, 'getuser', return_value='someone'), \
             mock.patch.object(sweep.socket, 'gethostname', return_value='host.example'):
            self.assertEqual(self.run_main(terms=None)[0], 1)

    def test_exit_codes(self):
        self.assertEqual(self.run_main(), (0, 'pii sweep: clean (0 files)\n'))
        self.assertEqual(self.run_main(['--unknown'])[0], 2)
        self.assertEqual(self.run_main(['--force'])[0], 2)
        self.assertEqual(self.run_main(['--help'])[0], 0)

    def commit(self, message='c'):
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def pre_push(self, lines, terms=('alice',), remote=None):
        out = io.StringIO()
        with mock.patch.object(sweep.sys, 'stdin', io.StringIO(lines)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            code = sweep.main(['--pre-push'] + (['--remote', remote] if remote else []), root=self.root,
                              terms=list(terms))
        return code, out.getvalue()

    def test_pre_push_reads_outgoing_commits_not_the_working_tree(self):
        zero = '0' * 40
        self.add('a.txt', 'safe\n')
        base = self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', base)
        self.add('a.txt', 'hello alice\n')
        self.commit()
        self.add('a.txt', 'safe again\n')
        tip = self.commit()  # the working file is clean; an outgoing commit is not
        self.assertEqual(self.run_main(terms=['alice'])[0], 0)
        code, out = self.pre_push(f'refs/heads/main {tip} refs/heads/main {base}\n')
        self.assertEqual(code, 1)
        self.assertIn(':a.txt:1: personal term', out)
        self.assertNotIn('alice', out)
        # A new branch: everything no remote has yet; a message names the person too.
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.add('b.txt', 'safe\n')
        msg_tip = self.commit('thanks alice')
        code, out = self.pre_push(f'refs/heads/x {msg_tip} refs/heads/x {zero}\n')
        self.assertEqual(code, 1)
        self.assertIn(':message:', out)
        # A deleted ref sends nothing; nothing outgoing is clean.
        self.assertEqual(self.pre_push(f'(delete) {zero} refs/heads/x {msg_tip}\n')[0], 0)
        self.assertEqual(self.pre_push(f'refs/heads/main {tip} refs/heads/main {tip}\n')[0], 0)

    def test_pre_push_merges_tags_and_the_destination_remote(self):
        zero = '0' * 40
        self.add('a.txt', 'safe\n')
        base = self.commit()
        self.git('switch', '-q', '-c', 'side')
        self.add('s.txt', 'side\n')
        self.commit()
        self.git('switch', '-q', '-')
        self.add('m.txt', 'main\n')
        self.commit()
        self.git('merge', '-q', '--no-commit', 'side')
        self.add('a.txt', 'hello alice\n')  # only in the merge's resolution
        self.git('commit', '-q', '-m', 'merge')
        merge = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('update-ref', 'refs/remotes/public/main', base)
        self.assertEqual(self.pre_push(f'refs/heads/main {merge} refs/heads/main {base}\n')[0], 1)
        # Commits another remote has are still new to this one.
        self.git('update-ref', 'refs/remotes/private/main', merge)
        self.assertEqual(self.pre_push(f'refs/heads/x {merge} refs/heads/x {zero}\n', remote='public')[0], 1)
        self.assertEqual(self.pre_push(f'refs/heads/x {merge} refs/heads/x {zero}\n', remote='private')[0], 0)
        # An annotated tag's own message.
        self.git('tag', '-a', 'v1', '-m', 'thanks alice', base)
        tag = self.git('rev-parse', 'v1').decode().strip()
        self.git('update-ref', 'refs/remotes/public/main', merge)
        code, out = self.pre_push(f'refs/tags/v1 {tag} refs/tags/v1 {zero}\n', remote='public')
        self.assertEqual(code, 1)
        self.assertIn(':tag:', out)

    def test_service_noreply_addresses_pass(self):
        for text in (b'Co-Authored-By: Bot <noreply@vendor.org>', b'from no-reply@service.org'):
            self.assertEqual(sweep.scan_text('a', text, []), [])

    def test_utf16_is_text(self):
        self.add('w.txt', 'hello alice\r\n'.encode('utf-16'))
        self.assertEqual(self.run_main(terms=['alice'])[0], 1)

    def test_terms_are_whole_words(self):
        self.assertEqual(sweep.scan_text('a', b'it cannot be', ['ann']), [])
        self.assertEqual(sweep.scan_text('a', b'ask Ann.', ['ann']), [('a', 1, 'personal term')])
        self.assertEqual(sweep.scan_text('a', b'call +1 555', ['+1 555']), [('a', 1, 'personal term')])

    def test_local_identity_email_and_ignore_mark(self):
        ex = ['maintainer@corp.org']  # pii-sweep: ignore
        self.assertEqual(sweep.scan_text('a', b'mail maintainer@corp.org', [], ex), [])  # pii-sweep: ignore
        self.assertEqual(sweep.scan_text('a', b'mail other@corp.org', [], ex), [('a', 1, 'email')])  # pii-sweep: ignore
        self.assertEqual(sweep.scan_text('a', b'x = "alice"  # pii-sweep: ignore', ['alice']), [])

    def test_hook_install_refusal_and_force(self):
        hook = self.root / '.git/hooks/pre-push'
        self.assertEqual(self.run_main(['--install-hook'])[0], 0)
        self.assertIn('python3 tools/pii_sweep.py --pre-push --remote "$1"', hook.read_text())
        if os.name != 'nt':  # Windows has no executable bit; Git for Windows runs the hook without one
            self.assertTrue(hook.stat().st_mode & 0o111)
        self.assertEqual(self.run_main(['--install-hook'])[0], 0)
        hook.write_text('#!/bin/sh\necho foreign\n', encoding='utf-8')
        self.assertEqual(self.run_main(['--install-hook'])[0], 2)
        self.assertIn('foreign', hook.read_text())
        self.assertEqual(self.run_main(['--install-hook', '--force'])[0], 0)


if __name__ == '__main__':
    unittest.main()
