"""Revision-bound Git reads and conditional merges. Never check out a user's tree."""

import hashlib
import os
import re
import subprocess


class GitReviewError(ValueError):
    pass


def git(path, *args, input=None, allowed=(0,)):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "diff.external=",
            *args,
        ],
        input=input,
        capture_output=True,
        timeout=45,
        env=env,
    )
    if result.returncode not in allowed:
        raise GitReviewError(
            result.stderr.decode("utf-8", "replace")[:700] or "Git operation failed"
        )
    return result


def registered_repository(paths, name, expected_path):
    """Resolve only a registered worker repository with the expected path/name."""
    if not isinstance(expected_path, str) or not os.path.isabs(expected_path):
        raise GitReviewError("Repository registration needs a current absolute path")
    expected = os.path.realpath(expected_path)
    candidates = {
        os.path.realpath(path)
        for path in paths
        if os.path.basename(os.path.normpath(path)) == name
        and os.path.realpath(path) == expected
    }
    if len(candidates) != 1:
        raise GitReviewError("Repository registration does not match this worker")
    return candidates.pop()


def branch(path, name):
    if not isinstance(name, str) or not name or len(name) > 255 or name.startswith("-"):
        raise GitReviewError("Choose a valid local branch name")
    git(path, "check-ref-format", "refs/heads/" + name)
    return "refs/heads/" + name


def resolve(path, ref):
    return git(path, "rev-parse", "--verify", ref + "^{commit}").stdout.decode().strip()


def compare(path, base, head, included=None):
    base_ref, head_ref = branch(path, base), branch(path, head)
    if base_ref == head_ref:
        raise GitReviewError("Choose two different branches")
    base_sha, head_sha = resolve(path, base_ref), resolve(path, head_ref)
    merge_base = git(path, "merge-base", base_sha, head_sha).stdout.decode().strip()
    patch = git(
        path,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--find-renames",
        "--unified=3",
        merge_base,
        head_sha,
        "--",
    ).stdout
    names = git(
        path,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        merge_base,
        head_sha,
        "--",
    ).stdout.split(b"\0")
    files = []
    i = 0
    while i < len(names) and names[i]:
        status = names[i].decode()
        i += 1
        old_path = names[i].decode("utf-8", "replace")
        i += 1
        new_path = old_path
        if status.startswith(("R", "C")):
            new_path = names[i].decode("utf-8", "replace")
            i += 1
        files.append({"status": status, "path": new_path, "old_path": old_path})
    history = git(
        path, "log", "--format=%H%x00%an%x00%s", base_sha + ".." + head_sha, "--"
    ).stdout.decode("utf-8", "replace")
    commits = []
    for line in history.splitlines():
        fields = line.split("\0", 2)
        if len(fields) == 3:
            commits.append(dict(zip(("sha", "author", "subject"), fields)))
    # merge-tree computes objects without a worktree or index. Conflicts are explicit.
    merged = git(path, "merge-tree", "--write-tree", base_sha, head_sha, allowed=(0, 1))
    included_results = []
    for item in included or []:
        sha = item.get("sha", "")
        if not re.fullmatch(r"[a-f0-9]{40,64}", sha):
            raise GitReviewError("Invalid included revision")
        contains = (
            git(
                path, "merge-base", "--is-ancestor", sha, head_sha, allowed=(0, 1)
            ).returncode
            == 0
        )
        included_results.append(
            {
                "number": item["number"],
                "sha": sha,
                "revision": item.get("revision"),
                "included": contains,
            }
        )
    limit = 1000000
    return {
        "included": included_results,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "patch": patch[:limit].decode("utf-8", "replace"),
        "truncated": len(patch) > limit,
        "patch_digest": hashlib.sha256(patch).hexdigest(),
        "files": files,
        "commits": commits[:500],
        "commits_truncated": len(commits) > 500,
        "conflicts": merged.returncode != 0,
        "merge_tree": merged.stdout.decode().splitlines()[0]
        if merged.returncode == 0
        else None,
    }


def merge(path, base, head, expected_base, expected_head, operation, actor):
    """Atomically verify both refs and write a two-parent merge on a bare authority.

    A durable receipt ref makes worker retries idempotent even if the target moves
    after a successful merge. The source ref is verified in the same transaction.
    """
    if git(path, "rev-parse", "--is-bare-repository").stdout.strip() != b"true":
        raise GitReviewError(
            "Native merge requires a bare authoritative repository; working copies are read-only"
        )
    if not re.fullmatch(r"[a-f0-9-]{36}", operation):
        raise GitReviewError("Invalid merge operation")
    for sha in (expected_base, expected_head):
        if not re.fullmatch(r"[a-f0-9]{40,64}", sha or ""):
            raise GitReviewError("Invalid reviewed revision")
    base_ref, head_ref = branch(path, base), branch(path, head)
    receipt = "refs/stargit/pr-merges/" + operation
    prior = git(path, "rev-parse", "--verify", receipt, allowed=(0, 128))
    if prior.returncode == 0:
        return {"merge_sha": prior.stdout.decode().strip(), "replayed": True}
    if (
        resolve(path, base_ref) != expected_base
        or resolve(path, head_ref) != expected_head
    ):
        raise GitReviewError(
            "Branches changed. Refresh and review the new revision before merging."
        )
    merged = git(
        path, "merge-tree", "--write-tree", expected_base, expected_head, allowed=(0, 1)
    )
    if merged.returncode:
        raise GitReviewError("Merge conflicts must be resolved on the source branch")
    tree = merged.stdout.decode().splitlines()[0]
    # Fixed service identity; human attribution is retained in the PR event ledger.
    result = git(
        path,
        "-c",
        "user.name=StarGit",
        "-c",
        "user.email=merges@stargit.local",
        "commit-tree",
        tree,
        "-p",
        expected_base,
        "-p",
        expected_head,
        input=f"Merge StarGit pull request\n\nOperation: {operation}\nApproved merge by user: {actor}\n".encode(),
    )
    sha = result.stdout.decode().strip()
    transaction = f"start\nverify {head_ref} {expected_head}\nupdate {base_ref} {sha} {expected_base}\ncreate {receipt} {sha}\nprepare\ncommit\n"
    git(path, "update-ref", "--stdin", input=transaction.encode())
    return {"merge_sha": sha, "replayed": False}


def valid_anchor(snapshot, path, line, side):
    if side not in ("old", "new") or type(line) is not int or line < 1:
        return False
    files = snapshot.get("files", [])
    sections = re.split(r"(?=^diff --git )", snapshot.get("patch", ""), flags=re.M)
    sections = [section for section in sections if section.startswith("diff --git ")]
    for file, section in zip(files, sections):
        if file["path"] != path:
            continue
        old = new = None
        for row in section.splitlines():
            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
            if match:
                old, new = map(int, match.groups())
                continue
            if old is None or not row or row.startswith("\\"):
                continue
            if row[0] == " ":
                if (old if side == "old" else new) == line:
                    return True
                old += 1
                new += 1
            elif row[0] == "-":
                if side == "old" and old == line:
                    return True
                old += 1
            elif row[0] == "+":
                if side == "new" and new == line:
                    return True
                new += 1
    return False
