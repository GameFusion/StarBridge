"""Bounded committed-tree snapshots for working copies and bare repositories."""
import subprocess


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, timeout=60).stdout


def file_snapshot(path, ref='HEAD'):
    sha = git(path, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    entries = git(path, 'ls-tree', '-r', '-l', '-z', sha)
    files, total = [], 0
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, blob, size = metadata.split()
        if kind != b'blob':
            continue
        size = int(size)
        total += size
        files.append(dict(name=name.decode('utf-8', 'replace'), blob_sha=blob.decode(),
                          size=size, latest_sha=sha, last_change_unknown=True))
    from repository_history import annotate_files
    annotate_files(path, sha, files)
    return files, total


def readme_snapshot(path, ref='HEAD'):
    sha = git(path, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    names = git(path, 'ls-tree', '--name-only', '-z', sha).split(b'\0')
    name = next((n for n in names if n.lower() in (b'readme.md', b'readme.markdown', b'readme')), None)
    if name is None:
        return ''
    object_name = sha + ':' + name.decode('utf-8')
    size = int(git(path, 'cat-file', '-s', object_name))
    if size > 500_000:
        return ''
    return git(path, 'show', object_name).decode('utf-8', 'replace')
