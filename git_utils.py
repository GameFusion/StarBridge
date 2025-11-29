import os
import subprocess
from subprocess import Popen, PIPE
import logging
from pathlib import Path

logger = logging.getLogger('StarBridge')

import settings

GIT_EXECUTABLE = settings.get("git_executable")

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

        result = subprocess.run(git_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        # STEP 1 — Find current branch (may be detached)
        r = subprocess.run(
            [git, "-C", repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            logger.info(f"[ahead/behind] Detached HEAD → return (0,0)")
            return 0, 0

        branch = r.stdout.strip()
        logger.debug(f"[ahead/behind] branch={branch}")

        # STEP 2 — Try explicit upstream
        upstream_r = subprocess.run(
            [git, "-C", repo_path, "rev-parse", "--abbrev-ref", f"{branch}@{{u}}"],
            capture_output=True, text=True, timeout=timeout
        )

        if upstream_r.returncode == 0:
            upstream = upstream_r.stdout.strip()
            logger.debug(f"[ahead/behind] upstream={upstream} (explicit)")
        else:
            logger.info(f"[ahead/behind] No upstream for '{branch}' → falling back")

            # STEP 3 — Fallback to origin/<branch>
            upstream = f"origin/{branch}"

            test = subprocess.run(
                [git, "-C", repo_path, "rev-parse", "--verify", "--quiet", upstream],
                capture_output=True, text=True, timeout=timeout
            )
            if test.returncode != 0:
                logger.info(f"[ahead/behind] '{upstream}' does not exist → scanning remotes")

                # STEP 4 — Try ANY remote that has this branch
                remotes = subprocess.run(
                    [git, "-C", repo_path, "remote"],
                    capture_output=True, text=True, timeout=timeout
                ).stdout.split()

                found = False
                for remote in remotes:
                    candidate = f"{remote}/{branch}"
                    chk = subprocess.run(
                        [git, "-C", repo_path, "rev-parse", "--verify", "--quiet", candidate],
                        capture_output=True, text=True, timeout=timeout
                    )
                    if chk.returncode == 0:
                        upstream = candidate
                        found = True
                        logger.debug(f"[ahead/behind] Using fallback remote branch: {upstream}")
                        break

                if not found:
                    logger.info(f"[ahead/behind] No remote branch found for '{branch}' → (0,0)")
                    return 0, 0

        # STEP 5 — Fetch only the needed remote
        remote = upstream.split("/")[0]
        # Safe fetch — never allowed to crash
        try:
            subprocess.run(
                [git, "-C", repo_path, "fetch", remote, "--quiet", "--no-tags", "--prune"],
                capture_output=True, text=True, timeout=timeout
            )
            logger.debug(f"[ahead/behind] fetch '{remote}' OK")
        except subprocess.TimeoutExpired:
            logger.warning(f"[ahead/behind] fetch '{remote}' TIMED OUT → continuing without fetch")
        except Exception as e:
            logger.warning(f"[ahead/behind] fetch '{remote}' failed ({type(e).__name__}) → {e}")

        # STEP 6 — Calculate ahead/behind (final robust step)
        rr = subprocess.run(
            [git, "-C", repo_path, "rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
            capture_output=True, text=True, timeout=timeout
        )

        if rr.returncode != 0:
            logger.warning(f"[ahead/behind] rev-list failed → return (0,0)")
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

    remote_name = "origin"
    remote_name = None

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"     # Disable HTTPS prompts
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"  # Disable SSH passphrases

    if remote_name:
        cmd = [GIT_EXECUTABLE, "-C", repo_path, "ls-remote", remote_name]
    else:
        cmd = [GIT_EXECUTABLE, "-C", repo_path, "ls-remote"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}

    if result.returncode != 0:
        return {"error": result.stderr.strip() or "ls-remote failed"}

    heads = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)

        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha

        elif ref.startswith(f"refs/remotes/{remote_name}/"):
            heads[ref[len(f"refs/remotes/{remote_name}/"):]] = sha

    return heads
