"""Committed path changes, scanned once per immutable tree revision.

First-parent history attributes changes introduced by a merge to that merge on
this branch. NUL-delimited raw records preserve literal tabs/newlines in paths.
"""
from functools import lru_cache
import subprocess


@lru_cache(maxsize=8)
def path_changes(path, sha):
    if not isinstance(sha, str) or len(sha) not in (40, 64) or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('An immutable commit ID is required')
    data = subprocess.run(
        ['git', '-C', str(path), 'log', '--first-parent', '--root',
         '--diff-merges=first-parent', '--no-renames', '--raw', '-z',
         '--format=%x00COMMIT%x00%H%x00%ai%x00%s%x00', sha, '--'],
        capture_output=True, check=True, timeout=60).stdout
    tokens = iter(data.split(b'\0'))
    changes, directories, commit = {}, {}, None
    for token in tokens:
        if token == b'COMMIT':
            commit = dict(sha=next(tokens).decode(), date=next(tokens).decode(),
                          message=next(tokens).decode('utf-8', 'replace'))
        elif token.lstrip(b'\n').startswith(b':'):
            name = next(tokens).decode('utf-8', 'replace')
            if commit is None:
                raise ValueError('Missing history record')
            changes.setdefault(name, commit)
            parts = name.split('/')
            for i in range(1, len(parts)):
                directories.setdefault('/'.join(parts[:i]), commit)
    return changes, directories


def annotate_files(path, sha, files):
    changes, directories = path_changes(str(path), sha)
    for entry in files:
        change = changes.get(entry['name'])
        if change:
            entry['last_change'] = dict(change)
            entry.pop('last_change_unknown', None)
    return directories
