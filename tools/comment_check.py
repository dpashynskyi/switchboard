"""List comments added between git trees, with history signs first."""
import argparse
import bisect
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tokenize

C_FAMILY = set('.c .cc .cpp .cxx .h .hh .hpp .inl .cs .java .js .jsx .mjs .ts .tsx .go .rs .swift .kt .usf .ush'.split())
HASH = set('.sh .bash .ps1 .psm1 .yml .yaml .toml .rb .pl .r'.split())
EXTENSIONS = sorted(C_FAMILY | HASH | {'.py', '.lua', '.sql'})
DIRECTIVES = ('noqa', 'type:', 'pylint:', 'pyright:', 'mypy:', 'fmt:', 'isort:', 'pragma',
              'nolint', 'eslint-', '@ts-', 'prettier-ignore', 'clang-format', 'iwyu pragma',
              'spdx-license-identifier', 'region', 'endregion', 'nosec')
MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
DATE = re.compile(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+' + MONTH + r'\s+\d{4}|' + MONTH + r'\s+(?:\d{1,2},?\s+)?\d{4})\b', re.I)
ROUND = re.compile(r'\b(?:r\d{1,4}|round\s+\d+)\b', re.I)
REVIEW = re.compile(r'\b(?:the\s+review|review\s+of|reviewer|a\s+review)\b', re.I)


def history_signs(text):
    return [name for name, pattern in (('date', DATE), ('round', ROUND), ('review', REVIEW)) if pattern.search(text)]


def _git(cwd, *args, env=None):
    return subprocess.run(['git', '-C', str(cwd), *args], env=env, capture_output=True,
                          check=True, timeout=60).stdout


def repo_top(cwd):
    try:
        return os.fsdecode(_git(cwd, 'rev-parse', '--show-toplevel')).strip()
    except Exception:
        return None


def snapshot(cwd):
    temporary = None
    try:
        top = _git(cwd, 'rev-parse', '--show-toplevel').decode().strip()
        index = Path(os.fsdecode(_git(top, 'rev-parse', '--git-path', 'index')).strip())
        if not index.is_absolute():
            index = Path(top) / index
        fd, temporary = tempfile.mkstemp(prefix='comment-index-')
        os.close(fd)
        if index.exists():
            shutil.copyfile(index, temporary)
        else:
            os.unlink(temporary)
        env = dict(os.environ, GIT_INDEX_FILE=temporary)
        pathspecs = [':(icase)*' + ext for ext in EXTENSIONS]
        paths = _git(top, 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
                     '--', *pathspecs, env=env).split(b'\0')
        present = {Path(os.fsdecode(path)).suffix.lower() for path in paths if path}
        # Git rejects unmatched pathspecs, even when another extension matches.
        pathspecs = [':(icase)*' + ext for ext in EXTENSIONS if ext in present]
        if pathspecs:
            _git(top, 'add', '-A', '--', *pathspecs, env=env)
        return _git(top, 'write-tree', env=env).decode().strip()
    except Exception:
        return None
    finally:
        if temporary:
            for path in (temporary, temporary + '.lock'):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _scan(text, extension):
    """Yield offsets, marker-free text and whether a comment fills its lines."""
    if extension == '.py':
        try:
            starts = [0] + [m.end() for m in re.finditer('\n', text)]
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
            for token in tokens:
                if token.type == tokenize.COMMENT:
                    start = starts[token.start[0] - 1] + token.start[1]
                    end = starts[token.end[0] - 1] + token.end[1]
                    yield start, end, token.string[1:], not text[starts[token.start[0] - 1]:start].strip(), True
            return
        except (tokenize.TokenError, IndentationError, SyntaxError):
            extension = '.sh'
    i, size = 0, len(text)
    while i < size:
        block = None
        marker = None
        if extension in C_FAMILY:
            raw = re.match(r'R"([^\s()\\]{0,16})\(', text[i:]) if text.startswith('R"', i) else None
            if raw:
                end = text.find(')' + raw[1] + '"', i + raw.end())
                i = size if end < 0 else end + len(raw[1]) + 2
                continue
            rust_raw = (re.match(r'b?r(#*)"', text[i:i + 40]) if extension == '.rs' and text[i] in 'br'
                        and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == '_')) else None)
            if rust_raw:
                end = text.find('"' + rust_raw[1], i + rust_raw.end())
                i = size if end < 0 else end + 1 + len(rust_raw[1])
                continue
            if text.startswith('/*', i):
                block = ('/*', '*/')
            elif text.startswith('//', i):
                marker = '//'
        elif extension in HASH:
            if extension in {'.ps1', '.psm1'} and text.startswith('<#', i):
                block = ('<#', '#>')
            elif text[i] == '#' and (i == 0 or text[i - 1].isspace()):
                marker = '#'
        elif text.startswith('--', i):
            marker = '--'
        if block or marker:
            full = not text[text.rfind('\n', 0, i) + 1:i].strip()
        if block:
            end = text.find(block[1], i + 2)
            stop = size if end < 0 else end + 2
            yield i, stop, text[i + 2:size if end < 0 else end], full, False
            i = stop
        elif marker:
            end = text.find('\n', i)
            end = size if end < 0 else end
            yield i, end, text[i + len(marker):end], full, True
            i = end
        elif text[i] in "\"'" or (text[i] == '`' and extension in {'.js', '.jsx', '.mjs', '.ts', '.tsx', '.go'}):
            quote = text[i]
            if extension == '.rs' and quote == "'" and not re.match(r"'(?:[^'\n\\]|\\.)'", text[i:i + 4]):
                i += 1
                continue
            if quote == "'" and extension in C_FAMILY:
                # A digit separator (1'000, 0xFF'FF), not a char literal (u8'a', L'a'): the token before starts with a digit.
                j = i
                while j > 0 and (text[j - 1].isalnum() or text[j - 1] in "_'"):
                    j -= 1
                if j < i and text[j].isdigit():
                    i += 1
                    continue
            go_raw = quote == '`' and extension == '.go'
            i += 1
            while i < size:
                if text[i] == '\\' and not go_raw:
                    i += 2
                elif text[i] == '\n' and quote != '`':
                    # An apostrophe in plain text ("it's" in YAML) must not hide the comments below it.
                    break
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
        else:
            i += 1


def _comments(text, extension):
    starts = [0] + [m.end() for m in re.finditer('\n', text)]
    comments = []
    for start, end, body, full, single in _scan(text, extension):
        line = bisect.bisect_right(starts, start)
        last = bisect.bisect_right(starts, max(start, end - 1))
        body = ' '.join(body.split())
        if (body.lower().startswith(DIRECTIVES) or (line == 1 and text[start:start + 2] == '#!')
                or (extension == '.py' and line <= 2 and re.search(r'coding[:=]\s*[-\w.]+', body))):
            continue
        if comments and full and single and comments[-1][3] and comments[-1][1] == line - 1:
            comments[-1][1] = last
            comments[-1][2] = ' '.join((comments[-1][2] + ' ' + body).split())
        else:
            comments.append([line, last, body, full and single])
    return comments


def added_comments(top, before, after):
    top = os.fsdecode(_git(top, 'rev-parse', '--show-toplevel')).strip()
    entries = []
    paths = _git(top, 'diff', '--name-only', '-z', '--no-renames', before, after).split(b'\0')
    for raw_path in paths:
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        extension = Path(path).suffix.lower()
        if extension not in EXTENSIONS:
            continue
        # A user's diff.interHunkContext would merge hunks and count unchanged lines between them as added.
        diff = _git(top, '-c', 'diff.interHunkContext=0', 'diff', '--unified=0', '--no-renames', '--no-color', before, after, '--', path).decode('utf-8', 'replace')
        added = set()
        for match in re.finditer(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', diff, re.M):
            start, count = int(match[1]), int(match[2] or 1)
            added.update(range(start, start + count))
        if not added:
            continue
        text = _git(top, 'show', f'{after}:{path}').decode('utf-8', 'replace')
        for first, last, body, _ in _comments(text, extension):
            if any(line in added for line in range(first, last + 1)):
                entries.append(dict(path=path, line=first, text=body, signs=history_signs(body)))
    return entries


def _ordered(entries):
    return sorted(entries, key=lambda entry: not bool(entry['signs']))


def _entry(entry, cut=False):
    text = entry['text']
    if cut and len(text) > 160:
        text = text[:157] + '...'
    signs = f" [{', '.join(entry['signs'])}]" if entry['signs'] else ''
    return f"  {entry['path']}:{entry['line']}{signs} {text}"


def report(entries, limit=12):
    if not entries:
        return ['[comments added: none]']
    history = sum(bool(entry['signs']) for entry in entries)
    counts = {name: sum(name in entry['signs'] for entry in entries) for name in ('date', 'round', 'review')}
    suffix = f", {history} with history signs (" + ', '.join(f'{name} {count}' for name, count in counts.items() if count) + ')' if history else ''
    lines = [f'[comments added: {len(entries)}{suffix}]']
    limit = max(0, limit)
    lines += [_entry(entry, cut=True) for entry in _ordered(entries)[:limit]]
    if len(entries) > limit:
        lines.append(f'  ... and {len(entries) - limit} more')
    return lines


def write_list(entries, path):
    Path(path).write_text(''.join(_entry(entry) + '\n' for entry in _ordered(entries)), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', default='.', help='repository folder')
    parser.add_argument('--json', action='store_true', help='print entries as JSON')
    parser.add_argument('--limit', type=int, default=12, help='maximum entries in the report')
    parser.add_argument('FROM', help='starting commit or tree')
    parser.add_argument('TO', nargs='?', help='ending commit or tree (default: working tree snapshot)')
    args = parser.parse_args(argv)
    try:
        top = _git(args.cwd, 'rev-parse', '--show-toplevel').decode().strip()
        before = _git(top, 'rev-parse', '--verify', '--end-of-options', args.FROM + '^{tree}').decode().strip()
        after = (_git(top, 'rev-parse', '--verify', '--end-of-options', args.TO + '^{tree}').decode().strip()
                 if args.TO else snapshot(top))
        if after is None:
            raise ValueError('working tree snapshot failed')
        entries = added_comments(top, before, after)
    except Exception as error:
        parser.error(str(error))
    print(json.dumps(entries, ensure_ascii=False) if args.json else '\n'.join(report(entries, args.limit)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
