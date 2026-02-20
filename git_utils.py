import os
import subprocess
from subprocess import Popen, PIPE
import logging
import shutil
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger('StarBridge')

import settings

configured_git = settings.get("git_executable", "git")
if configured_git and os.path.isabs(configured_git) and os.path.exists(configured_git):
    GIT_EXECUTABLE = configured_git
else:
    GIT_EXECUTABLE = shutil.which(configured_git or "git") or shutil.which("git") or "git"

if GIT_EXECUTABLE != configured_git:
    logger.warning(
        "Configured git_executable '%s' not found; using '%s' instead",
        configured_git,
        GIT_EXECUTABLE
    )

def is_file_tracked(repo_path: str | Path, file_path: str | Path) -> bool:
    """
    Return True if the file is tracked by Git.
    Fast, accurate, uses Git's own index.
    """
    repo_path = Path(repo_path).resolve()
    file_path = Path(file_path).resolve()

    # Must be inside repo
    if not file_path.is_relative_to(repo_path):
        return False

    rel_path = file_path.relative_to(repo_path)

    # Fast path: use git ls-files (cached, instant)
    result = subprocess.run(
        ["git", "-C", str(repo_path), "ls-files", "--error-unmatch", str(rel_path)],
        capture_output=True,
        text=True
    )
    return result.returncode == 0


def compute_diff_stats(diff_chunks):
    added = 0
    removed = 0

    for chunk in diff_chunks:
        # Split into lines safely
        lines = chunk.split("\n")

        for line in lines:
            # Ignore metadata lines
            if line.startswith("---") or line.startswith("+++"):
                continue
            if line.startswith("@@"):
                continue

            # Count added/removed lines
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1

    return added, removed

def get_diff(repo_path):
    try:
        #print("processing diff for repo", repo_path, flush=True)
        git_command = [GIT_EXECUTABLE, "-C", repo_path, "diff"]

        result = subprocess.run(
            git_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if result.returncode != 0:
            #print("NO DIFF", flush=True)
            return {}
        else:
            diff_output = result.stdout

            # Default stats
            added = 0
            removed = 0
            if diff_output:
                added, removed = compute_diff_stats(diff_output)

            return {
                "diff":diff_output,
                "lines_added": added,
                "lines_removed": removed
            }
        
    except Exception as e:
        logger.error("get_diff(): Exception computing diff: %s", str(e))
        return {}


def get_ahead_behind(repo_path, git="git", timeout=10):
    """
    Fully failsafe ahead/behind resolver.
    Handles: no upstream, mismatched names, no remotes, detached HEAD.
    """
    logger.debug(f"[ahead/behind] repo={repo_path}")

    try:
        # STEP 1 - Find current branch (may be detached)
        r = subprocess.run(
            [git, "-C", repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
        if r.returncode != 0:
            logger.info("[ahead/behind] Detached HEAD -> return (0,0)")
            return 0, 0

        branch = r.stdout.strip()
        logger.debug(f"[ahead/behind] branch={branch}")

        # STEP 2 - Try explicit upstream
        upstream_r = subprocess.run(
            [git, "-C", repo_path, "rev-parse", "--abbrev-ref", f"{branch}@{{u}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )

        if upstream_r.returncode == 0:
            upstream = upstream_r.stdout.strip()
            logger.debug(f"[ahead/behind] upstream={upstream} (explicit)")
        else:
            logger.info(f"[ahead/behind] No upstream for '{branch}' -> falling back")

            # STEP 3 - Fallback to origin/<branch>
            upstream = f"origin/{branch}"

            test = subprocess.run(
                [git, "-C", repo_path, "rev-parse", "--verify", "--quiet", upstream],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout
            )
            if test.returncode != 0:
                logger.info(f"[ahead/behind] '{upstream}' does not exist -> scanning remotes")

                # STEP 4 - Try any remote that has this branch
                remotes = subprocess.run(
                    [git, "-C", repo_path, "remote"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout
                ).stdout.split()

                found = False
                for remote in remotes:
                    candidate = f"{remote}/{branch}"
                    chk = subprocess.run(
                        [git, "-C", repo_path, "rev-parse", "--verify", "--quiet", candidate],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout
                    )
                    if chk.returncode == 0:
                        upstream = candidate
                        found = True
                        logger.debug(f"[ahead/behind] Using fallback remote branch: {upstream}")
                        break

                if not found:
                    logger.info(f"[ahead/behind] No remote branch found for '{branch}' -> (0,0)")
                    return 0, 0

        # STEP 5 - Fetch only the needed remote
        remote = upstream.split("/")[0]
        # Safe fetch - never allowed to crash
        try:
            subprocess.run(
                [git, "-C", repo_path, "fetch", remote, "--quiet", "--no-tags", "--prune"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout
            )
            logger.debug(f"[ahead/behind] fetch '{remote}' OK")
        except subprocess.TimeoutExpired:
            logger.warning(f"[ahead/behind] fetch '{remote}' TIMED OUT -> continuing without fetch")
        except Exception as e:
            logger.warning(f"[ahead/behind] fetch '{remote}' failed ({type(e).__name__}) -> {e}")

        # STEP 6 - Calculate ahead/behind (final robust step)
        rr = subprocess.run(
            [git, "-C", repo_path, "rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )

        if rr.returncode != 0:
            logger.warning("[ahead/behind] rev-list failed -> return (0,0)")
            return 0, 0

        behind, ahead = map(int, rr.stdout.strip().split("\t"))
        return ahead, behind

    except Exception as e:
        logger.exception(f"[ahead/behind] Unexpected error: {e}")
        return 0, 0

def get_remote_heads(repo_path, timeout=3):
    """
    Safely return dict of remote refs:
    {
        'main': 'abc123...',
        'feature/login': 'def456...'
    }

    Never blocks thanks to:
    - timeout
    - GIT_TERMINAL_PROMPT=0
    - BatchMode=yes (no SSH password prompts)
    """

    details = get_remote_heads_details(repo_path, timeout=timeout)
    return details.get("canonical_heads", {})


def _parse_heads_from_ls_remote(stdout):
    heads = {}
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha
    return heads


def _pick_canonical_remote(remotes):
    """
    Pick canonical remote with fallback:
    1) Prefer healthy origin (no error + has heads)
    2) Else first healthy remote (no error + has heads)
    3) Else origin if present (for stability/debug visibility)
    4) Else first available remote
    """
    if not remotes:
        return None

    def is_healthy(name):
        data = remotes.get(name) or {}
        heads = data.get("heads") or {}
        err = data.get("error")
        return (not err) and bool(heads)

    if "origin" in remotes and is_healthy("origin"):
        return "origin"

    for name in remotes.keys():
        if is_healthy(name):
            return name

    if "origin" in remotes:
        return "origin"

    return next(iter(remotes), None)


def get_remote_heads_details(repo_path, timeout=3):
    """
    Collect remote heads per remote without failing the whole result if one remote is bad.

    Returns:
    {
      "fetched_at": "...Z",
      "canonical_remote": "origin",
      "canonical_heads": {"main": "..."},
      "remotes": {
         "origin": {"heads": {...}, "error": "...", "url_fetch": "...", "url_push": "..."},
         "github": {"heads": {...}, "error": None, ...}
      }
    }
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"

    # Discover remotes and urls first.
    remotes_result = subprocess.run(
        [GIT_EXECUTABLE, "-C", repo_path, "remote", "-v"],
        capture_output=True,
        text=True
    )

    remotes = {}
    if remotes_result.returncode == 0:
        for line in remotes_result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, url, typ = parts[0], parts[1], parts[2].strip("()")
            if name not in remotes:
                remotes[name] = {"heads": {}, "error": None, "url_fetch": None, "url_push": None}
            if typ == "fetch":
                remotes[name]["url_fetch"] = url
            elif typ == "push":
                remotes[name]["url_push"] = url

    # If no remotes discovered, return empty stable structure.
    if not remotes:
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "canonical_remote": None,
            "canonical_heads": {},
            "remotes": {}
        }

    # Query each remote independently to avoid one failure poisoning all.
    for remote_name in remotes.keys():
        try:
            result = subprocess.run(
                [GIT_EXECUTABLE, "-C", repo_path, "ls-remote", remote_name],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if result.returncode != 0:
                remotes[remote_name]["error"] = result.stderr.strip() or "ls-remote failed"
                remotes[remote_name]["heads"] = {}
                continue

            remotes[remote_name]["heads"] = _parse_heads_from_ls_remote(result.stdout)
            remotes[remote_name]["error"] = None
        except subprocess.TimeoutExpired:
            remotes[remote_name]["error"] = "timeout"
            remotes[remote_name]["heads"] = {}
        except Exception as e:
            remotes[remote_name]["error"] = str(e)
            remotes[remote_name]["heads"] = {}

    canonical_remote = _pick_canonical_remote(remotes)
    canonical_heads = remotes.get(canonical_remote, {}).get("heads", {}) if canonical_remote else {}

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "canonical_remote": canonical_remote,
        "canonical_heads": canonical_heads,
        "remotes": remotes
    }

def get_current_commit_sha(repo_path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except:
        return "unknown"
