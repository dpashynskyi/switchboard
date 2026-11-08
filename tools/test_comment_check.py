"""Offline comment check tests using disposable repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'skills' / 'ask'))
import comment_check
import route
import writer


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='comment-check-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.repo = self.folder / 'repo'
        self.repo.mkdir()
        self.hooks = self.folder / 'hooks'
        self.hooks.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture.invalid',
                        GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture.invalid',
                        GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0',
                        TMP=str(self.folder), TEMP=str(self.folder),
                        TMPDIR=str(self.folder))
        for key in ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR',
                    'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
            self.env.pop(key, None)
        self.environment = mock.patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tempdir_patch = mock.patch.object(tempfile, 'tempdir', str(self.folder))
        self.tempdir_patch.start()
        self.addCleanup(self.tempdir_patch.stop)
        self.git('init', '--quiet')
        self.write('seed.py', 'value = 1\n')
        self.first = self.commit()

    def git(self, *args):
        return subprocess.run(
            ['git', '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false',
             '-c', 'core.hooksPath=' + str(self.hooks), '-C', str(self.repo), *args],
            env=self.env, capture_output=True, check=True, timeout=60).stdout

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode('utf-8'))

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD').decode().strip()

    def snapshot(self, cwd=None):
        tree = comment_check.snapshot(cwd or self.repo)
        self.assertIsNotNone(tree)
        return tree

    def entries(self, before):
        return comment_check.added_comments(self.repo, before, self.snapshot())

    def test_python_comments_not_strings_docstrings_or_directives(self):
        before = self.snapshot()
        self.write('new.py', '# a comment\nx = "# not a comment"\n"""a docstring"""\n# noqa: E501\n')
        self.assertEqual(self.entries(before), [
            {'path': 'new.py', 'line': 1, 'text': 'a comment', 'signs': []}])

    def test_cpp_paragraph_block_raw_string_and_trailing_comment(self):
        before = self.snapshot()
        self.write('new.cpp', '// one\n// two\nconst char* url = "http://x";\n'
                   '/* a\n b */\nauto raw = R"(// no)";\nx = 1; // why\n')
        self.assertEqual(self.entries(before), [
            {'path': 'new.cpp', 'line': 1, 'text': 'one two', 'signs': []},
            {'path': 'new.cpp', 'line': 4, 'text': 'a b', 'signs': []},
            {'path': 'new.cpp', 'line': 7, 'text': 'why', 'signs': []}])

    def test_unchanged_comment_next_to_added_line_and_changed_comment(self):
        self.write('change.py', '# unchanged\nvalue = 1\n\n# old\n')
        before = self.snapshot()
        self.write('change.py', '# unchanged\nvalue = 2\n\n# changed\n')
        self.assertEqual(self.entries(before), [
            {'path': 'change.py', 'line': 4, 'text': 'changed', 'signs': []}])

    def test_whole_new_text_preserves_multiline_string_and_block_context(self):
        self.write('block.cpp', '/* original\n old\n */\n')
        self.write('string.py', 'value = """\nold\n"""\n')
        before = self.snapshot()
        self.write('block.cpp', '/* original\n changed\n */\n')
        self.write('string.py', 'value = """\n# still a string\n"""\n')
        self.assertEqual(self.entries(before), [
            {'path': 'block.cpp', 'line': 1, 'text': 'original changed', 'signs': []}])

    def test_uppercase_extension_is_seen(self):
        before = self.snapshot()
        self.write('upper.CPP', '// round 3\n')
        self.assertEqual(self.entries(before), [
            {'path': 'upper.CPP', 'line': 1, 'text': 'round 3', 'signs': ['round']}])

    def test_inter_hunk_context_does_not_count_lines_between_changes(self):
        self.git('config', 'diff.interHunkContext', '1')
        self.write('hunks.py', 'x = 1\n# unchanged\ny = 1\n')
        before = self.snapshot()
        self.write('hunks.py', 'x = 2\n# unchanged\ny = 2\n')
        self.assertEqual(self.entries(before), [])

    def test_untracked_ignored_and_non_code_files(self):
        self.write('.gitignore', 'ignored.py\n')
        before = self.snapshot()
        self.write('new.py', '# visible\n')
        self.write('ignored.py', '# hidden\n')
        self.write('notes.md', '# heading\n')
        self.assertEqual([e['path'] for e in self.entries(before)], ['new.py'])

    def test_snapshot_leaves_real_index_and_status_unchanged_with_staged_changes(self):
        self.write('seed.py', '# staged\nvalue = 2\n')
        self.write('staged.md', 'staged non-code\n')
        self.git('add', 'seed.py', 'staged.md')
        self.write('seed.py', '# working tree\nvalue = 3\n')
        self.write('untracked.py', '# untracked\n')
        index = self.repo / '.git' / 'index'
        before_index = index.read_bytes()
        before_status = self.git('status', '--porcelain')
        tree = self.snapshot()
        self.assertEqual(index.read_bytes(), before_index)
        self.assertEqual(self.git('status', '--porcelain'), before_status)
        self.assertEqual(self.git('show', tree + ':seed.py'), b'# working tree\nvalue = 3\n')

    def test_snapshot_leaves_clean_index_and_status_unchanged(self):
        index = self.repo / '.git' / 'index'
        before_index = index.read_bytes()
        before_status = self.git('status', '--porcelain')
        self.snapshot()
        self.assertEqual(index.read_bytes(), before_index)
        self.assertEqual(self.git('status', '--porcelain'), before_status)

    def test_snapshot_without_index_and_without_code_files(self):
        (self.repo / '.git' / 'index').unlink()
        self.snapshot()
        self.assertFalse((self.repo / '.git' / 'index').exists())
        self.git('reset', '--mixed', 'HEAD')
        before = self.snapshot()
        (self.repo / 'seed.py').unlink()
        after = self.snapshot()
        self.assertEqual(comment_check.added_comments(self.repo, before, after), [])
        self.snapshot()

    def test_snapshot_outside_repository(self):
        outside = self.folder / 'outside'
        outside.mkdir()
        # Stop discovery before the enclosing workspace repository.
        with mock.patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.folder)}):
            self.assertIsNone(comment_check.snapshot(outside))

    def test_snapshot_from_subfolder_and_added_comments_from_subfolder(self):
        sub = self.repo / 'src'
        sub.mkdir()
        before = self.snapshot(sub)
        self.write('src/new.py', '# nested\n')
        after = self.snapshot(sub)
        self.assertEqual(comment_check.added_comments(sub, before, after), [
            {'path': 'src/new.py', 'line': 1, 'text': 'nested', 'signs': []}])

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, '-B', str(HERE / 'comment_check.py'),
                               '--cwd', str(self.repo), *args], env=self.env,
                              capture_output=True, text=True, encoding='utf-8',
                              check=check, timeout=60)

    def test_cli_between_commits_and_bad_from(self):
        self.write('added.py', '# a comment\n')
        second = self.commit()
        result = self.cli(self.first, second)
        self.assertEqual(result.returncode, 0)
        self.assertIn('[comments added: 1]', result.stdout)
        self.assertIn('added.py:1 a comment', result.stdout)
        bad = self.cli('not-a-valid-revision', second, check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn('error:', bad.stderr)

    def test_cli_default_snapshot_json_limit_and_usage_error(self):
        self.write('added.py', '# round 3\n')
        result = self.cli('--json', self.first)
        self.assertEqual(json.loads(result.stdout), [
            {'path': 'added.py', 'line': 1, 'text': 'round 3', 'signs': ['round']}])
        result = self.cli('--limit', '0', self.first)
        self.assertIn('... and 1 more', result.stdout)
        self.assertEqual(self.cli(check=False).returncode, 2)

    def test_journal_comments_are_written_only_when_given(self):
        path = str(self.folder / 'journal.jsonl')
        counts = {'added': 3, 'history': 1}
        route.journal('writer.py', 'writer', 'model', 'codex', 'model',
                      comments=counts, journal_path=path, rules={})
        route.journal('writer.py', 'writer', 'model', 'codex', 'model',
                      journal_path=path, rules={})
        rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0]['comments'], counts)
        self.assertNotIn('comments', rows[1])


class CommentTests(unittest.TestCase):
    def test_history_signs(self):
        cases = [
            ('(PRX-35 r99, 7 November 2026)', ['date', 'round']),
            ('the review of r99', ['round', 'review']),
            ('ptr3', []), ('r2d2', []), ('2026 cm', []),
            ('2026-11-07', ['date']), ('Nov 7, 2026', ['date']),
            ('November 2026', ['date']), ('round 3', ['round']),
            ('reviewer and a review', ['review']),
            ('r99 r98 round 3 the review review of', ['round', 'review']),
            ('some_r99 r12345 reviewable', []),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(comment_check.history_signs(text), expected)

    def texts(self, text, extension):
        return [(entry[0], entry[2]) for entry in comment_check._comments(text, extension)]

    def test_digit_separators_rust_raw_strings_and_go_raw_strings(self):
        self.assertEqual(self.texts("auto n = 1'000'000; // yes\nchar c = u8'a'; // also\n", '.cpp'),
                         [(1, 'yes'), (2, 'also')])
        self.assertEqual(self.texts('let t = r#"a " // no"#; // yes\n', '.rs'), [(1, 'yes')])
        self.assertEqual(self.texts('var t = `a\n// no\n` // yes\n', '.go'), [(3, 'yes')])

    def test_apostrophe_in_plain_text_does_not_hide_later_comments(self):
        self.assertEqual(self.texts("title: it's here\n# yes\n", '.yaml'), [(2, 'yes')])
        self.assertEqual(self.texts("echo don't\n# yes\n", '.sh'), [(2, 'yes')])

    def test_hash_scanners_powershell_and_dash_comments(self):
        for extension in comment_check.HASH:
            with self.subTest(extension=extension):
                self.assertEqual(self.texts('x = "# no"\ny = \'# no\'\nx#no\n# yes\n', extension), [(4, 'yes')])
        for extension in ('.ps1', '.psm1'):
            self.assertEqual(self.texts('<# a\n b #>\nx = 1 # tail\n', extension), [(1, 'a b'), (3, 'tail')])
        for extension in ('.lua', '.sql'):
            self.assertEqual(self.texts('x = "-- no"\n-- yes\n-- more\n', extension), [(2, 'yes more')])
        self.assertEqual(self.texts('`cmd` # yes\n', '.sh'), [(1, 'yes')])

    def test_c_family_scanners_skip_quoted_escapes_and_raw_strings(self):
        for extension in comment_check.C_FAMILY:
            with self.subTest(extension=extension):
                text = 'x = "escaped \\" // no";\ny = \'/\';\n// yes\n'
                self.assertEqual(self.texts(text, extension), [(3, 'yes')])
        self.assertEqual(self.texts('auto x = R"tag( // no\n/* no */)tag"; // yes\n', '.cpp'), [(2, 'yes')])
        for extension in ('.js', '.jsx', '.mjs', '.ts', '.tsx'):
            self.assertEqual(self.texts('x = `// no\n/* no */`; // yes\n', extension), [(2, 'yes')])
        self.assertEqual(self.texts("fn f<'a>(x: &'a str) { // yes\n}\nlet x = '\\n'; // tail\n", '.rs'),
                         [(1, 'yes'), (3, 'tail')])

    def test_python_tokenization_fallback(self):
        self.assertEqual(self.texts('x = (\n# fallback\n', '.py'), [(2, 'fallback')])

    def test_directives_shebang_and_coding_line(self):
        for prefix in comment_check.DIRECTIVES:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.texts('# ' + prefix.upper() + ' detail\n', '.py'), [])
        self.assertEqual(self.texts('#!/usr/bin/env python\n# coding: utf-8\n# actual\n', '.py'), [(3, 'actual')])
        self.assertEqual(self.texts('// SPDX-License-Identifier: MIT\n// actual\n', '.cpp'), [(2, 'actual')])

    def test_paragraphs_and_trailing_comments_stay_separate(self):
        text = '# one\n# two\n\n# three\nx = 1 # tail\n# four\n'
        self.assertEqual(self.texts(text, '.py'), [(1, 'one two'), (4, 'three'), (5, 'tail'), (6, 'four')])

    def test_report_none_counts_order_cut_and_more(self):
        self.assertEqual(comment_check.report([]), ['[comments added: none]'])
        entries = [dict(path='plain.py', line=1, text='plain', signs=[]),
                   dict(path='history.py', line=2, text='x' * 161, signs=['date', 'round']),
                   dict(path='review.py', line=3, text='the review', signs=['review'])]
        lines = comment_check.report(entries, limit=1)
        self.assertEqual(lines[0], '[comments added: 3, 2 with history signs (date 1, round 1, review 1)]')
        self.assertEqual(lines[1], '  history.py:2 [date, round] ' + 'x' * 157 + '...')
        self.assertEqual(len(lines[1].split('] ', 1)[1]), 160)
        self.assertEqual(lines[2], '  ... and 2 more')
        self.assertEqual(comment_check.report(entries[:1]), ['[comments added: 1]', '  plain.py:1 plain'])
        self.assertEqual(comment_check.report(entries[1:2])[0],
                         '[comments added: 1, 1 with history signs (date 1, round 1)]')
        entries[0]['text'] = 'z' * 160
        self.assertTrue(comment_check.report(entries[:1])[1].endswith('z' * 160))

    def test_write_list_is_complete_utf8_and_history_first(self):
        entries = [dict(path='plain.py', line=1, text='plain', signs=[]),
                   dict(path='history.py', line=2, text='\u03bb' * 200, signs=['round'])]
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder) / 'comments.txt'
            comment_check.write_list(entries, path)
            self.assertEqual(path.read_text(encoding='utf-8').splitlines(), [
                '  history.py:2 [round] ' + '\u03bb' * 200, '  plain.py:1 plain'])

    def test_snapshot_failures_never_raise(self):
        for error in (FileNotFoundError('git missing'), subprocess.TimeoutExpired('git', 60),
                      subprocess.CalledProcessError(1, 'git')):
            with self.subTest(error=type(error).__name__), mock.patch.object(comment_check, '_git', side_effect=error):
                self.assertIsNone(comment_check.snapshot(ROOT))

    def test_writer_report_and_failure_are_advisory(self):
        entries = [dict(path='code.py', line=1, text='round 3', signs=['round'])]
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            base = str(Path(folder) / 'run')
            with mock.patch.object(comment_check, 'snapshot', return_value='after'), \
                 mock.patch.object(comment_check, 'added_comments', return_value=entries):
                kwargs, lines = writer._comment_result(str(ROOT), 'before', base)
            self.assertEqual(kwargs, {'comments': {'added': 1, 'history': 1}})
            self.assertEqual(lines[-1], '  list in ' + base + '.comments.txt')
            self.assertTrue(Path(base + '.comments.txt').is_file())
            with mock.patch.object(comment_check, 'snapshot', side_effect=RuntimeError('failed')):
                self.assertEqual(writer._comment_result(str(ROOT), 'before', base),
                                 ({}, ['[comments added: not checked (failed)]']))
            self.assertEqual(writer._comment_result('outside', None, base),
                             ({}, ['[comments added: not checked (no git repository at outside)]']))


if __name__ == '__main__':
    unittest.main()
