"""Check tracked text for personal data without printing the matched data.

    python3 tools/pii_sweep.py               # the files git tracks, as they are on disk
    python3 tools/pii_sweep.py --staged      # the staged blobs
    python3 tools/pii_sweep.py --pre-push    # the commits a push sends (git's pre-push lines on stdin)
    python3 tools/pii_sweep.py --install-hook
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DROP = {'com', 'org', 'net', 'gmail', 'local', 'users', 'pro', 'macbook'}
SAFE_NAMES = {'someone', 'me', 'user', 'you', 'example', 'shared', 'public', 'runner', 'x'}
# An address has a dot in its domain: "person@exam" cut mid-word or "someone@sub." + a variable is no address.
EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@([\w-]+(?:\.[\w-]+)+)", re.UNICODE)
# A home folder at the start of a path, not "/home/" inside another one ("/outside/home/new.txt").
HOME_PATH = re.compile(r'(?<![\w.-])(?:/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]+)([^/\\\s\'"`:,;)<>|]+)', re.IGNORECASE)
HOOK_MARK = '# switchboard pii-sweep hook'
IGNORE = 'pii-sweep: ignore'
# A service's no-reply address (a co-author trailer's, say) names no person.
SERVICE = re.compile(r'(?i)no-?reply@')
HOOK = '''#!/bin/sh
# switchboard pii-sweep hook
if command -v python3 >/dev/null 2>&1; then
    python3 tools/pii_sweep.py --pre-push --remote "$1"
else
    python tools/pii_sweep.py --pre-push --remote "$1"
fi
exit $?
'''


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def config(root, scope, key):
    try:
        return git(root, 'config', scope, '--get', key).decode('utf-8', 'replace').strip()
    except subprocess.CalledProcessError:
        return ''


def machine_terms(home, config_dir, login, hostname, identity):
    """Collect only machine-owned sources; parameters keep tests independent of the host."""
    values = [login]
    values.extend(identity)
    for value in identity:
        if '@' in value:
            values.append(value.split('@', 1)[0])
    # The hostname only whole: its parts can be plain words ("Quiet_Hill" would flag every "quiet").
    terms = {h.casefold() for h in (hostname, re.sub(r'[ _.]', '-', hostname or '')) if h}
    for value in values:
        if value:
            terms.add(value.casefold())
            terms.update(p.casefold() for p in re.findall(r'[^\W\d_]{3,}', value) if p.casefold() not in DROP)
    try:
        data = json.loads((Path(config_dir) / 'machine.json').read_text(encoding='utf-8'))
        terms.update(v.casefold() for v in data.get('work_domains', []) if isinstance(v, str) and v)
    except (OSError, ValueError, AttributeError):
        pass
    for path in (Path(config_dir) / 'pii_deny.txt', Path(home) / '.config/pii-guard/patterns.txt'):
        try:
            terms.update(line.strip().casefold() for line in path.read_text(encoding='utf-8').splitlines()
                         if line.strip() and not line.lstrip().startswith('#'))
        except OSError:
            pass
    return terms


def public_exemptions(root):
    values = [config(root, '--local', key) for key in ('user.name', 'user.email')]
    origin = config(root, '--local', 'remote.origin.url')
    match = re.search(r'github\.com[:/]([^/\s]+/[^/\s]+)', origin, re.IGNORECASE)
    if match:
        address = match.group(1)
        if address.lower().endswith('.git'):
            address = address[:-4]
        values.append('github.com/' + address)
    return [v for v in values if v]


def without_public(text, exemptions):
    for value in sorted(exemptions, key=len, reverse=True):
        suffix = r'(?:\.git)?(?![\w.-]|/)' if value.lower().startswith('github.com/') else r'(?![\w@./:-])'
        prefix = r'(?<![\w@.-])' if value.lower().startswith('github.com/') else r'(?<![\w@./:-])'
        text = re.sub(prefix + re.escape(value) + suffix, '', text, flags=re.IGNORECASE)
    return text


def reserved(domain):
    domain = domain.casefold().rstrip('.')
    return (domain in {'example.com', 'example.org', 'example.net', 'localhost', 'users.noreply.github.com'}
            or domain.endswith(('.example', '.invalid', '.test', '.localhost', '.example.com', '.example.org',
                                '.example.net')))


def text_of(data):
    """The text of a blob, or None for a binary one. UTF-16 (a BOM; Windows tools write it) is text too."""
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16', 'replace')
    if b'\0' in data[:8192]:
        return None
    return data.decode('utf-8', 'replace')


def has_term(text, patterns):
    text = text.casefold()
    return any(p.search(text) for p in patterns)


def term_patterns(terms):
    # A term is a whole word where it starts or ends with a letter or digit: a login "ann" is not in "cannot".
    out = []
    for term in sorted({t.casefold() for t in terms if t}):
        left = r'(?<![^\W_])' if re.match(r'[^\W_]', term) else ''
        right = r'(?![^\W_])' if re.search(r'[^\W_]$', term) else ''
        out.append(re.compile(left + re.escape(term) + right))
    return out


def scan_text(path, data, terms, exemptions=()):
    text = text_of(data)
    if text is None:
        return []
    patterns = term_patterns(terms)
    hits = []
    if has_term(without_public(path, exemptions), patterns):
        hits.append((path, 1, 'personal term'))
    for number, raw in enumerate(text.splitlines(), 1):
        if IGNORE in raw:
            continue  # a test's own made-up fixture, marked as such
        line = without_public(raw, exemptions)
        if any(not reserved(m.group(1)) and not SERVICE.match(m.group(0)) for m in EMAIL.finditer(line)):
            hits.append((path, number, 'email'))
        for match in HOME_PATH.finditer(line):
            name = match.group(1)
            if (name.casefold() not in SAFE_NAMES and not name.startswith(('<', '$', '{', '%'))
                    and any(c.isalpha() for c in name)):
                hits.append((path, number, 'home path'))
                break
        if has_term(line, patterns):
            hits.append((path, number, 'personal term'))
    return sorted(set(hits), key=lambda h: (h[1], h[2]))


def tracked_files(root, staged=False):
    if staged:
        for entry in git(root, 'ls-files', '-s', '-z').split(b'\0'):
            if not entry:
                continue
            meta, path = entry.split(b'\t', 1)
            mode, oid, stage = meta.split()
            if stage != b'0':
                raise ValueError('unmerged index')
            if mode == b'160000':
                continue
            yield os.fsdecode(path), git(root, 'cat-file', 'blob', oid.decode('ascii'))
    else:
        for path in git(root, 'ls-files', '-z').split(b'\0'):
            if path:
                name = os.fsdecode(path)
                target = Path(root) / name
                # Never follow a tracked symlink outside the repository.
                if target.is_symlink():
                    yield name, os.fsencode(os.readlink(target))
                elif target.is_file():
                    yield name, target.read_bytes()


ZERO = re.compile(r'^0+$')


def outgoing(root, lines, remote=None):
    """(label, data) of what a push sends: each new commit's message and the blobs it adds or changes. lines are
    git's pre-push input: <local ref> <local sha> <remote ref> <remote sha>."""
    seen = set()
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or ZERO.match(parts[1]):
            continue  # a deleted ref sends nothing
        local, theirs = parts[1], parts[3]
        if git(root, 'cat-file', '-t', local).strip() == b'tag':
            yield f'{local[:10]}:tag', git(root, 'cat-file', 'tag', local)  # an annotated tag's message and tagger
        # A new ref: what the destination remote does not have (another remote's commits may be private there).
        known = ['--remotes=' + remote] if remote else ['--remotes']
        spec = [local, '--not', *known] if ZERO.match(theirs) else [local, '^' + theirs]
        for commit in git(root, 'rev-list', *spec).decode('ascii').split():
            if commit in seen:
                continue
            seen.add(commit)
            short = commit[:10]
            yield f'{short}:message', git(root, 'log', '-1', '--format=%an%n%ae%n%cn%n%ce%n%B', commit)
            # -m: a merge against each parent too, so a conflict's resolution is read.
            out = git(root, 'diff-tree', '-r', '-m', '--root', '--no-commit-id', '-z', '--diff-filter=AMRCT', commit)
            fields = out.split(b'\0')
            i = 0
            while i < len(fields) and fields[i]:
                meta = fields[i].split()
                status = meta[4].decode('ascii')
                i += 1
                if status[0] in 'RC':
                    i += 1  # the old path
                path = os.fsdecode(fields[i])
                i += 1
                if meta[1] == b'160000':
                    continue
                yield f'{short}:{path}', git(root, 'cat-file', 'blob', meta[3].decode('ascii'))


def install_hook(root, force=False):
    hook = Path(os.fsdecode(git(root, 'rev-parse', '--git-path', 'hooks/pre-push')).strip())
    if not hook.is_absolute():
        hook = Path(root) / hook
    if hook.exists() and HOOK_MARK not in hook.read_text(encoding='utf-8', errors='replace') and not force:
        raise ValueError('refusing to overwrite a foreign pre-push hook (use --force)')
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK, encoding='utf-8')
    hook.chmod(0o755)


def main(argv=None, root=ROOT, terms=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='scan staged blobs instead of working files')
    parser.add_argument('--pre-push', action='store_true',
                        help="scan the commits being pushed (git's pre-push lines on stdin), messages included")
    parser.add_argument('--remote', help="with --pre-push: the remote's name, as git passes it to the hook")
    parser.add_argument('--install-hook', action='store_true')
    parser.add_argument('--force', action='store_true', help='replace a foreign hook')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code
    if (args.force and not args.install_hook) or (args.staged and args.pre_push):
        parser.print_usage(sys.stderr)
        return 2
    try:
        if args.install_hook:
            install_hook(root, args.force)
            print('pii sweep: installed pre-push hook')
            return 0
        if terms is None:
            home = Path.home()
            config_dir = os.path.expanduser(os.environ.get('ASK_CONFIG_DIR') or str(home / '.config/ask'))
            terms = machine_terms(home, config_dir, getpass.getuser(), socket.gethostname(),
                                  [config(root, '--global', key) for key in ('user.name', 'user.email')])
        exemptions = public_exemptions(root)
        hits, count = [], 0
        source = outgoing(root, sys.stdin.read().splitlines(), args.remote) if args.pre_push else tracked_files(root, args.staged)
        for path, data in source:
            if text_of(data) is None:
                continue
            count += 1
            hits.extend(scan_text(path, data, terms, exemptions))
        for path, line, kind in hits:
            print(f'{path}:{line}: {kind}')
        if not hits:
            print(f'pii sweep: clean ({count} files)')
        return 1 if hits else 0
    except (OSError, subprocess.CalledProcessError, ValueError):
        print('pii sweep: unable to read repository/configuration or install hook', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
