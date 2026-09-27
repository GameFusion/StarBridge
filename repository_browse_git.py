"""Read a branch without checking it out or changing a working copy."""
import subprocess


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, timeout=60).stdout


def branch_snapshot(path, branch):
    # Only local branch refs, never revision expressions or command options.
    ref = 'refs/heads/' + str(branch)
    git(path, 'check-ref-format', ref)
    names = git(path, 'for-each-ref', '--format=%(refname)', 'refs/heads/').decode().splitlines()
    if ref not in names:
        raise ValueError('This branch is no longer available. Refresh the repository.')
    sha = git(path, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    files, size = [], 0
    for entry in git(path, 'ls-tree', '-r', '-l', '-z', sha).split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, blob, length = metadata.split()
        if kind != b'blob':
            continue
        length = int(length)
        size += length
        files.append(dict(name=name.decode('utf-8', 'replace'), size=length,
                          blob_sha=blob.decode(), latest_sha=sha, last_change_unknown=True))
    readme_name = next((f['name'] for f in files if f['name'].lower() in
                       ('readme.md', 'readme.markdown', 'readme') and f['size'] <= 500000), None)
    readme = git(path, 'show', sha + ':' + readme_name).decode('utf-8', 'replace') if readme_name else ''
    # Full history metadata is already inexpensive compared with per-file blame.
    commits = []
    for row in git(path, 'log', '--format=%H%x00%an%x00%ae%x00%ai%x00%P%x00%s', sha).decode('utf-8', 'replace').splitlines():
        values = row.split('\0', 5)
        if len(values) == 6:
            h, author, email, date, parents, message = values
            commits.append(dict(sha=h, author_name=author, author_email=email,
                                date=date, parents=parents.split(), message=message))
    return dict(branch=branch, sha=sha, files=files, readme=readme, commits=commits,
                storage_size=size)
