"""Bounded, read-only commit evidence shared by local and UUID-bound workers."""
import hashlib
import re
from pr_git import git, GitReviewError


def exact_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value):
        raise GitReviewError('An exact commit SHA is required.')
    return value


def read_commit(path, sha, patch=False):
    exact_sha(sha)
    resolved = git(path, 'rev-parse', '--verify', sha+'^{commit}').stdout.decode().strip()
    if resolved != sha:
        raise GitReviewError('Requested object is not an exact commit.')
    if patch:
        content = git(path, 'show', '--format=', '--no-ext-diff', '--no-textconv', '--no-renames', '--binary', sha, '--').stdout
        if len(content) > 2_000_000:
            raise GitReviewError('Patch exceeds 2 MB.')
        return dict(sha=sha, patch=content.decode('utf-8'), sha256=hashlib.sha256(content).hexdigest(), source='git-object')
    values = git(path, 'show', '-s', '--format=%H%x00%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B', sha).stdout.decode('utf-8', 'replace').split('\0', 9)
    if len(values) != 10:
        raise GitReviewError('Invalid commit metadata.')
    h, tree, parents, author, email, date, committer, committer_email, committed_at, message = values
    return dict(sha=h, tree_sha=tree, parents=parents.split(), author=dict(name=author, email=email, date=date),
                committer=dict(name=committer, email=committer_email, date=committed_at), message=message.rstrip(), source='git-object')
