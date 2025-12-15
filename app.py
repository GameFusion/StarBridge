
from flask import Flask, request, jsonify, abort, make_response, send_file
import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response
from subprocess import Popen, PIPE
import subprocess
import os
import re
import uuid
import time
import json
import requests
import logging
from logging.handlers import RotatingFileHandler
import threading
import psutil
from datetime import datetime, timedelta, timezone
import mimetypes
import base64 
import socket
import watchdog_live_diff
import signal
import shutil

# auto-setup, will create .env and settings.json if not present
import setup
import git_utils


# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('starbridge.log', maxBytes=1000000, backupCount=5),
        logging.StreamHandler()
    ]
)

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

logger = logging.getLogger('StarBridge')

# Global pause state
_pause_event = threading.Event()  # Clear = running, Set = paused

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Load settings from the JSON file
import settings

GIT_EXECUTABLE = settings.get("git_executable", "git")
REPOSITORIES = settings.get("repositories", [])
CERT_PATH = settings.get("ssl.cert_path", "certs/cert.pem")      # dot notation
KEY_PATH = settings.get("ssl.key_path", "certs/key.pem")
REPO_BASE = settings.get('repo_base') # REPO_BASE needs to explicitly be defined in settings to allow starbridge to create new repositories in poll mode

STARGIT_API_KEY = os.getenv('STARGIT_API_KEY', '')
STARGIT_API_URL = os.getenv('STARGIT_API_URL', 'https://stargit.com')
STARBRIDGE_PORT = os.getenv('STARBRIDGE_PORT', 5001)
ADMIN_PORT = os.getenv('ADMIN_PORT', 5002)
SERVER_UUID = os.getenv("STARBRIDGE_SERVER_UUID")
AUTH_ENDPOINT = f"{STARGIT_API_URL}/api/auth/token"
REGISTER_ENDPOINT = f"{STARGIT_API_URL}/api/servers/register"
HEARTBEAT_ENDPOINT = f"{STARGIT_API_URL}/api/servers/heartbeat"
POLL_ENDPOINT = f"{STARGIT_API_URL}/api/servers/poll"

GIT_VERBOSE_MODE = os.getenv("GIT_VERBOSE", "false").lower() in ("1", "true", "yes", "on")

PUSH_MODE = os.getenv('PUSH_MODE', 'false').lower() == 'true'
if not PUSH_MODE:
    logger.info("PUSH_MODE is disabled in .env; repository details will not be pushed to StarGit during heartbeats.")
else:
    logger.info("PUSH_MODE is enabled in .env; repository details will be pushed to StarGit during heartbeats.")

# Token storage (in-memory)
tokens = {
    'access_token': None,
    'refresh_token': None,
    'expires_at': None,
    'api_key_uuid': None  # Store APIKey.uuid as server_uuid
}

SSL_MODE = os.getenv('SSL_MODE', 'none').lower()

# Load the API key from environment variable
API_KEY = os.getenv('STARBRIDGE_API_KEY')
if not API_KEY:
    logger.error("STARBRIDGE_API_KEY not set in .env file")
    raise RuntimeError("STARBRIDGE_API_KEY not set in .env file. Run `python setup.py` to generate one.")

# API key check that aborts if the key is invalid (no return to parent function)
def check_api_key():
    logger.debug("Checking API key")
    xapi = request.headers.get('x-api-key')
    auth = request.headers.get('Authorization')

    if xapi:
        if xapi != API_KEY:
            logger.warning("Invalid API key provided")
            abort(401, description="Unauthorized access, invalid API key")
        # Valid x-api-key; proceed
    elif auth:
        logger.debug("Checking StarGit token via %s", STARGIT_URL)
        headers = {
            'Authorization': auth,
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(STARGIT_URL, headers=headers)
            if response.status_code != 200:
                logger.warning("Invalid StarGit token, status code: %s", response.status_code)
                abort(401, description="Unauthorized access, invalid Token")
        except requests.exceptions.HTTPError as err:
            logger.error("HTTP error during token validation: %s", err)
            abort(401, description=f"Error occurred: {err}")
        except Exception as e:
            logger.error("Unexpected error during token validation: %s", e)
            abort(500, description="An unexpected error occurred during token validation")
    else:
        logger.warning("No API key or token provided")
        abort(401, description="Unauthorized access, no API key or token provided")

def validate_session(lock_path, session_id):
    """
    Validate a push session by checking the lock file and session ID.

    Parameters:
    - lock_path: Path to the lock file (e.g., local_lock_path).
    - session_id: The session ID to validate.

    Returns:
    - Tuple: (is_valid, error_message). If valid, returns (True, None), else returns (False, "Error message").
    """
    logger.debug("Validating session for lock_path: %s, session_id: %s", lock_path, session_id)
    # Check if the lock file exists
    if not os.path.exists(lock_path):
        error_msg = f"Missing lock for valid push session on {lock_path}."
        logger.error(error_msg)
        return False, error_msg

    # Read the lock file and check the session ID
    try:
        with open(lock_path, 'r') as lock_file:
            lock_data = json.load(lock_file)
            # Ensure the session_id in the lock matches the provided session_id
            if lock_data.get('session_id') != session_id:
                error_msg = f"Invalid session ID {session_id} for lock at {lock_path}."
                logger.warning(error_msg)
                return False, error_msg
    except Exception as e:
        # If there is any issue reading or parsing the lock file, return an error
        error_msg = f"Error validating session: {str(e)}"
        return False, error_msg

    # If all checks pass, return valid
    logger.debug("Session validated successfully")
    return True, None

def run_git_command(path, command):
    """Utility function to run a git command and return the output."""
    if GIT_VERBOSE_MODE:
        logger.debug("Running git command: %s in path: %s", command, path)
    try:
        result = subprocess.run(command, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        if GIT_VERBOSE_MODE:
            logger.debug("Git command output: %s", result.stdout.strip())
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error("Git command error: %s", e.stderr.strip())
        return f"Error: {e.stderr.strip()}"

@app.route('/api/refs', methods=['POST'])
def get_refs():
    logger.info("API endpoint /api/refs called")
    check_api_key()  # Your function to verify the API key
    data = request.json
    repo_path = data.get('repo_path')
    logger.debug("Processing refs for repo_path: %s", repo_path)

    if repo_path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Get local refs
    local_refs = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "show-ref"])
    local_refs_info = []
    
    for ref in local_refs.splitlines():
        if ref.strip():  # Check if the line is not empty
            sha, name = ref.split(' ', 1)
            local_refs_info.append({
                "name": name,
                "sha": sha
            })

    # Get remote refs
    remote_refs = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "ls-remote"])
    remote_refs_info = []
   
    for ref in remote_refs.splitlines():
        if ref.strip():  # Check if the line is not empty
            # Split by whitespace to handle multiple spaces or tabs
            parts = ref.split()
            if len(parts) == 2:  # Ensure there are exactly 2 parts
                sha, name = parts
                remote_refs_info.append({
                    "name": name,
                    "sha": sha
                })
            else:
                logger.warning("Unexpected format in remote refs line: %s", ref)

    result = {
        "local_refs": local_refs_info,
        "remote_refs": remote_refs_info
    }
    logger.debug("Returning refs: %s", result)
    return jsonify(result), 200

@app.route('/api/add', methods=['POST'])
def git_add_file():
    logger.info("API endpoint /api/add called")
    check_api_key()  # Your function to verify the API key
    data = request.json
    repo_path = data.get('repo_path')
    path_file_name_to_add = data.get('path_file')
    logger.debug("Adding file: %s in repo: %s", path_file_name_to_add, repo_path)

    # Validate inputs
    if not repo_path or not path_file_name_to_add:
        logger.warning("Missing required parameters: repo_path or path_file")
        return jsonify({"error": "Both 'repo_path' and 'path_file' are required"}), 400

    if repo_path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Run git add command
    try:
        print("Adding file to repository...", flush=True)
        add_result = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "add", path_file_name_to_add])
        if add_result.strip():  # Check if git add produced any output
            logger.debug("Git add output: %s", add_result)
        logger.info("File '%s' added successfully", path_file_name_to_add)
        return jsonify({"success": True, "message": f"File '{path_file_name_to_add}' added successfully"}), 200
    except Exception as e:
        logger.error("Failed to add file '%s': %s", path_file_name_to_add, str(e))
        return jsonify({"error": f"Failed to add file '{path_file_name_to_add}': {str(e)}"}), 500

def get_branches_data(repo_path):
    """
    Returns local and remote branches for a given repo path.
    Returns:
        (branches_data, error)
        branches_data = {
            "local_branches": [{"name": "main"}, ...],
            "remote_branches": [{"name": "origin/main"}, ...]
        }
        error = None if success, otherwise {"type": str, "message": str}
    """
    if repo_path not in REPOSITORIES:
        return None, {"type": "NotFound", "message": f"Repository path '{repo_path}' not found in registered repositories"}

    try:
        # Local branches
        local_branches = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "branch", "--format", "%(refname:short)"])
        local_branches_info = [{"name": b.strip()} for b in local_branches.splitlines() if b.strip()]

        # Remote branches
        remote_branches = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "branch", "-r", "--format", "%(refname:short)"])
        remote_branches_info = [{"name": b.strip()} for b in remote_branches.splitlines() if b.strip()]

        branches_data = {
            "local_branches": local_branches_info,
            "remote_branches": remote_branches_info
        }

        return branches_data, None

    except subprocess.CalledProcessError as e:
        return None, {"type": "GitError", "message": str(e)}
    except Exception as e:
        return None, {"type": "Exception", "message": str(e)}
    
@app.route('/api/branch', methods=['POST'])
def get_branches():
    logger.info("API endpoint /api/branch called")
    check_api_key()
    data = request.json
    repo_path = data.get('repo_path')

    branches_data, error = get_branches_data(repo_path)
    if error:
        logger.error("Error retrieving branches for %s: %s", repo_path, error["message"])
        return jsonify(error), 500

    return jsonify(branches_data), 200

### TODO REMOVE DEPRECATED AFTER CLEANUP
@app.route('/api/branch_dep', methods=['POST'])
def get_branches_deprected():
    logger.info("API endpoint /api/branch called")
    check_api_key()  # Verify the API key
    data = request.json
    repo_path = data.get('repo_path')
    logger.debug("Fetching branches for repo_path: %s", repo_path)

    # Check if the repository exists
    if repo_path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Get local branches
    try:
        local_branches = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "branch", "--format", "%(refname:short)"])
    except subprocess.CalledProcessError as e:
        logger.error("Failed to retrieve local branches: %s", e)
        return jsonify({"error": f"Failed to retrieve local branches: {e}"}), 500

    # Parse local branches
    local_branches_info = []
    for branch in local_branches.splitlines():
        if branch.strip():
            local_branches_info.append({
                "name": branch.strip()
            })

    # Get remote branches
    try:
        remote_branches = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "branch", "-r", "--format", "%(refname:short)"])
    except subprocess.CalledProcessError as e:
        logger.error("Failed to retrieve remote branches: %s", e)
        return jsonify({"error": f"Failed to retrieve remote branches: {e}"}), 500

    # Parse remote branches
    remote_branches_info = []
    for branch in remote_branches.splitlines():
        if branch.strip():
            remote_branches_info.append({
                "name": branch.strip()
            })

    result = {
        "local_branches": local_branches_info,
        "remote_branches": remote_branches_info
    }

    logger.debug("Returning branches: %s", result)
    # Return the branch information
    return jsonify(result), 200


@app.route('/api/remotes', methods=['POST'])
def get_remotes():
    logger.info("API endpoint /api/remotes called")
    check_api_key()  # Verify the API key
    data = request.json
    repo_path = data.get('repo_path')
    logger.debug("Fetching remotes for repo_path: %s", repo_path)

    # Check if the repository exists
    if repo_path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Get list of remotes
    try:
        remotes = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "remote", "-v"])
    except subprocess.CalledProcessError as e:
        logger.error("Failed to retrieve remotes: %s", e)
        return jsonify({"error": f"Failed to retrieve remotes: {e}"}), 500

    # Parse remote information
    remotes_info = []
    for line in remotes.splitlines():
        if line.strip():  # Ensure the line is not empty
            parts = line.split()
            if len(parts) >= 2:  # Expected format: <name> <url> (fetch/push)
                remote_name, remote_url = parts[0], parts[1]
                remote_type = parts[2].strip("()") if len(parts) > 2 else "unknown"
                
                # Append remote information to the list
                remotes_info.append({
                    "name": remote_name,
                    "url": remote_url,
                    "type": remote_type
                })

    # Return remote information
    result = {
        "remotes": remotes_info
    }
    logger.debug("Returning remotes: %s", result)
    return jsonify(result), 200

@app.route('/api/revwalk', methods=['POST'])
def rev_walk():
    logger.info("API endpoint /api/revwalk called")
    check_api_key()  # Your function to verify the API key
    data = request.json
    branch = data.get('branch')
    path = data.get('repo_path')
    mod = data.get('mod', None)  # Optional
    logger.debug("Processing revwalk for repo_path: %s, branch: %s", path, branch)
    
    if not path:
        logger.warning("Repository path is required")
        branch 

    # Path to repo (adjust if multiple repos)
    if path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", path)
        return jsonify({"error": f"Repository path '{path}' not found in registered repositories"}), 400

    repo_path = os.path.join(path, ".git")

    # Git command to get commit details with parents, author info, date, and message
    command = [GIT_EXECUTABLE, "-C", path, "log", f"--date=iso", "--pretty=format:%H|%P|%an|%ae|%ad|%s", branch]

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        # for initial commit
        return jsonify({"commits": [{
            "sha": "00000000",
            "parents": "",
            "author_name": "",
            "author_email": "",
            "date": "",
            "message": "Initial commit"
        }]}), 200
        print("Error on git log", stderr.decode('utf-8'), flush=True)
        return jsonify({"error": "Error running git log", "details": stderr.decode('utf-8')}), 500

    # Parse the output (SHA with parents, author, email, date, and message)
    commits = stdout.decode('utf-8').splitlines()

    # Return structure
    commit_list = []
    for commit_line in commits:
        # Splitting each line based on the '|' delimiter added in --pretty=format
        sha, parents, author_name, author_email, date, message = commit_line.split("|", 5)
        parents_list = parents.split() if parents else []

        commit_info = {
            "sha": sha,
            "parents": parents_list,
            "author_name": author_name,
            "author_email": author_email,
            "date": date,
            "message": message
        }
        commit_list.append(commit_info)

    logger.debug("Returning %d commits", len(commit_list))
    return jsonify({"commits": commit_list}), 200

@app.route('/api/diff', methods=['POST'])
def diff():
    """
    API to get the diff of uncommitted changes, a single commit, or between two commits.
    If a single commit is provided, also return the commit details (message, author, email, date, parents).
    """
    logger.info("API endpoint /api/diff called")
    check_api_key()
    data = request.json
    path = data.get('repo_path')
    commit = data.get('commit')
    commit1 = data.get('commit1')
    commit2 = data.get('commit2')

    # Define a size limit for the diff output (e.g., 5 MB)
    DIFF_CUTOFF_SIZE = 1 * 1024 * 1024  # 5 MB in bytes

    if not repo_path:
        logger.warning("Repository path is required")
        return jsonify({"error": "Repository path is required"}), 400

    if path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{path}' not found in registered repositories"}), 400

    try:
        if not os.path.isdir(path):
            logger.error("Repository path '%s' does not exist", path)
            raise Exception(f"Repository path {path} does not exist")

        # Prepare the git command to get the diff
        git_command = [GIT_EXECUTABLE, "-C", path, "diff"]

        if commit1 and commit2:
            # Get diff between two commits
            git_command.extend([commit1, commit2])
        elif commit1:
            # Get diff for a single commit
            git_command.append(commit1)
        elif commit:
            # Get diff for the single commit
            git_command.append(commit)
        # If no commit1 or commit2 is provided, it defaults to the working directory diff (uncommitted changes)

        # Run the git diff command
        result = subprocess.run(git_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            logger.error("Error running git diff: %s", result.stderr)
            raise Exception(f"Error running git diff: {result.stderr}")

        diff_output = result.stdout
        original_diff_size = len(diff_output)

        # Check if the diff exceeds the cutoff size and truncate if necessary
        truncated = False
        if original_diff_size > DIFF_CUTOFF_SIZE:
            diff_output = diff_output[:DIFF_CUTOFF_SIZE]
            truncated = True

        logger.debug("Diff length: %d -> %d, truncated: %s", original_diff_size, len(diff_output), truncated)

        commit_details = None
        if commit:
            # Get the commit details (message, author, email, date, and parents)
            commit_details_command = [
                GIT_EXECUTABLE, "-C", path, "show", "--no-patch", "--format=%B%n%an%n%ae%n%ad%n%P", commit
            ]
            result_message = subprocess.run(commit_details_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if result_message.returncode != 0:
                logger.error("Error getting commit details: %s", result_message.stderr)
                raise Exception(f"Error getting commit details: {result_message.stderr}")

            commit_output = result_message.stdout.strip().splitlines()

            commit_message = commit_output[0]
            author_name = commit_output[1]
            author_email = commit_output[2]
            commit_date = commit_output[3]
            parents = commit_output[4] if len(commit_output) > 4 else None

            commit_details = {
                "commit_message": commit_message,
                "author": author_name,
                "email": author_email,
                "date": commit_date,
                "parents": parents
            }

        # Return the diff result and commit details if applicable
        return jsonify({
            "diff": diff_output,
            "commit_details": commit_details,
            "diff_info": {
                "original_size": original_diff_size,
                "cutoff_limit": DIFF_CUTOFF_SIZE,
                "truncated": truncated,
                "status": "truncated" if truncated else "complete"
            }
        }), 200

    except Exception as e:
        logger.error("Error in /api/diff: %s", str(e))
        return jsonify({"error": str(e)}), 500

@app.route('/api/push', methods=['POST'])
def push():
    """
    API to push changes to a remote repository.
    Expects JSON with the repo_path and optional branch.
    """
    logger.info("API endpoint /api/push called")
    check_api_key()  # Function to verify the API key

    data = request.json
    repo_path = data.get('repo_path')
    branch = data.get('branch', 'master')  # Default to 'main' if branch is not specified
    remote = data.get('remote', 'origin') # Default to 'origin' if remote is not specified

    logger.debug("Pushing to data: %s, branch: %s, remote: %s", data, branch, remote)

    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    try:
        # Prepare the git push command
        git_push_command = [GIT_EXECUTABLE, "-C", repo_path, "push", remote, branch]

        # Run the git push command
        result = subprocess.run(git_push_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            logger.error("Error pushing changes: %s", result.stderr.strip())
            raise Exception(f"Error pushing changes: {result.stderr.strip()}")

        # Return success response
        return jsonify({
            "status": "Push successful",
            "branch": branch
        }), 200

    except Exception as e:
        logger.error("Push exception generated: %s", str(e))
        return jsonify({
            "error": "Push command failed",
            "details": str(e)
        }), 500

@app.route('/api/pull', methods=['POST'])
def git_pull():
    """
    Perform a git pull operation on the specified repository.
    """
    logger.info("API endpoint /api/pull called")
    # Verify API key (if implemented in your application)
    check_api_key()  
    # Parse request data
    data = request.json
    repo_path = data.get('repo_path')
    pull_mode = data.get('pull_mode')

    # Validate input
    if not repo_path:
        logger.error("'repo_path' is required")
        return jsonify({"error": "'repo_path' is required"}), 400
    
    # Validate input
    if not pull_mode:
        logger.error("'pull_mode' is required")
        return jsonify({"error": "'pull_mode' is required"}), 400

    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Run git pull command
    try:
        logger.debug("Running git pull for repository at %s with mode %s", repo_path, pull_mode)
        pull_result = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "pull", pull_mode])
        logger.debug("Git pull output: %s", pull_result)
        
        return jsonify({
            "success": True,
            "message": "Git pull executed successfully",
            "output": pull_result
        }), 200
    except Exception as e:
        logger.error("Error during git pull: %s", str(e))
        return jsonify({"error": f"Failed to pull repository '{repo_path}': {str(e)}"}), 500
    
@app.route('/api/add-remote', methods=['POST'])
def add_remote():
    """
    API to add a new remote to a Git repository.
    Example: git remote add origin git@stargit.gamefusion.io:GameFusion/Python/StarBridge.git
    """
    logger.info("API endpoint /api/add-remote called")
    check_api_key()
    data = request.json
    remote_name = data.get('remote_name', 'origin')
    remote_url = data.get('remote_url')
    repo_path = data.get('repo_path')

    if not remote_url:
        logger.error("Remote URL is required")
        return jsonify({"error": "Remote URL is required"}), 400

    if not repo_path or repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    try:
        # Run git remote add command
        result = subprocess.run(
            [GIT_EXECUTABLE, "remote", "add", remote_name, remote_url],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            logger.error("Error adding remote: %s", result.stderr.strip())
            raise Exception(f"Error adding remote: {result.stderr}")

        # Return success response
        return jsonify({
            "status": "Remote added successfully",
            "remote_name": remote_name,
            "remote_url": remote_url
        }), 200

    except Exception as e:
        # Handle errors during remote add
        logger.error("Exception adding remote: %s", str(e))
        return jsonify({
            "error": str(e)
        }), 500

@app.route('/api/commit', methods=['POST'])
def commit():
    """
    API to stage changes and commit with the provided message.
    Returns the new commit ID on success.
    """
    logger.info("API endpoint /api/commit called")
    check_api_key()
    data = request.json
    name = data.get('name')
    email = data.get('email')
    path = data.get('repo_path')
    commit_message = data.get('message', 'Default commit message')
    logger.debug("Creating commit in repo_path: %s with message: %s", repo_path, message)
    
    if not repo_path or not message:
        logger.warning("Both 'repo_path' and 'message' are required")
        return jsonify({"error": "Both 'repo_path' and 'message' are required"}), 400

    if path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", path) 
        return jsonify({"error": f"Repository path '{path}' not found in registered repositories"}), 400
    
    try:
        # Stage all changes
        #subprocess.run([GIT_EXECUTABLE, "add", "--all"], cwd=REPO_PATH, check=True)

        # Commit with the provided message
        author_info = f"{name} <{email}>"
        subprocess.run([GIT_EXECUTABLE, "commit", "--author", author_info, "-a", "-m", commit_message], cwd=path, check=True)
        #subprocess.run([GIT_EXECUTABLE, "commit", "-m", commit_message], cwd=path, check=True)

        # Get the latest commit ID
        result = subprocess.run([GIT_EXECUTABLE, "rev-parse", "HEAD"], cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        commit_id = result.stdout.decode().strip()

        # Return success response with the new commit ID
        return jsonify({
            "status": "Commit successful",
            "commit_id": commit_id
        }), 200

    except subprocess.CalledProcessError as e:
        # Handle Git command errors
        logger.error("Git command failed: %s", e.stderr.decode().strip() if e.stderr else str(e))   
        return jsonify({
            "error": "Git command failed",
            "details": e.stderr.decode() if e.stderr else str(e)
        }), 500

def get_git_status_data(repo_path, git_executable="git"):
    """
    Full Git status with staged/unstaged/conflicts support using porcelain=v2.
    """
    logger.debug(f"Fetching git status for {repo_path}")
    try:
        # Use porcelain=v2 with -z for machine-readable, unambiguous output
        # Use "--untracked-files=all" to list all untracked files
        result = subprocess.run(
            [git_executable, "-C", repo_path, "status", "--porcelain=v2", "-z", "--branch"],
            capture_output=True,
            text=True,
            check=True
        )
        logger.debug(f"Git status raw output ({len(result.stdout)} chars)")
        entries = [e for e in result.stdout.split('\0') if e]

        summary = {
            "conflicts": [],
            "staged": [],      # Changes in index (to be committed)
            "unstaged": [],    # Changes in working tree (not staged)
            "untracked": [],
            "renamed": [],
            "added": [],       # New files staged
            "modified": [],    # Modified files (legacy)
            "deleted": [],      # Deleted files
            "ahead": 0,
            "behind": 0
        }

        merge_head = os.path.exists(os.path.join(repo_path, '.git', 'MERGE_HEAD'))
        rebase_merge = os.path.exists(os.path.join(repo_path, '.git', 'rebase-merge'))
        rebase_apply = os.path.exists(os.path.join(repo_path, '.git', 'rebase-apply'))
        merge_in_progress = merge_head or rebase_merge or rebase_apply

        branch_info = ""
        logger.debug(f"Processing {len(entries)} git status entries")

        for i, entry in enumerate(entries):
            if not entry.strip():
                logger.debug(f"Skipping empty entry at index {i}")
                continue

            parts = entry.split()
            if len(parts) < 2:
                logger.warning(f"Malformed git status entry at index {i}: '{entry}' — skipping")
                continue

            code = parts[0]
            logger.debug(f"Entry {i}: code='{code}', full='{entry}'")

            try:
                if code == "#":
                    if parts[1].startswith("branch.head"):
                        branch_info = " ".join(parts[2:])
                        logger.debug(f"Detected branch: {branch_info}")
                    continue

                if code in ("1", "2"):
                    xy = parts[1]
                    # Safe path extraction
                    path_parts = parts[8:] if len(parts) > 8 else parts[8:9] if len(parts) > 8 else [parts[-1]]
                    path = " ".join(path_parts)

                    # Staged
                    if xy[0] != ".":
                        if xy[0] == "A":
                            summary["added"].append(path)
                        elif xy[0] == "M":
                            summary["staged"].append(path)
                        elif xy[0] == "D":
                            summary["deleted"].append(path)
                        elif xy[0] == "R":
                            try:
                                old, new = path.split(" -> ")
                                summary["renamed"].append({"from": old.strip(), "to": new.strip()})
                                summary["staged"].append(new.strip())
                            except:
                                logger.warning(f"Failed to parse rename: {path}")

                    # Unstaged
                    if xy[1] != "." and xy[1] in ("M", "D"):
                        summary["unstaged"].append(path)

                elif code == "u":
                    path = " ".join(parts[8:]) if len(parts) > 8 else parts[-1]
                    summary["conflicts"].append(path)

                elif code == "?":
                    path = " ".join(parts[1:])
                    summary["untracked"].append(path)

            except Exception as e:
                logger.error(f"CRASH parsing entry {i}: '{entry}' — {e}")
                continue  # Don't die — keep going
        # Compute ahead/behind — only for current branch
        logger.debug("Calculating ahead/behind status")
        ahead, behind = git_utils.get_ahead_behind(repo_path, git_executable)
        summary["ahead"] = ahead
        summary["behind"] = behind

        # Smart action message
        logger.debug("Generating action summary and message")
        conflict_count = len(summary["conflicts"])
        staged_count = len(summary["staged"]) + len(summary["added"]) + len(summary["deleted"]) + len(summary["renamed"])
        unstaged_count = len(summary["unstaged"])

        if conflict_count > 0:
            action_summary = "Merge conflict"
            action_message = f"{conflict_count} conflicted file(s). Resolve to continue."
        elif merge_in_progress:
            if unstaged_count > 0:
                action_summary = "Merge in progress"
                action_message = f"{unstaged_count} unstaged change(s) remain. Stage or discard to continue."
            elif staged_count > 0:
                action_summary = "Ready to continue"
                action_message = f"All conflicts resolved. {staged_count} file(s) staged. Click Continue Merge to finish."
            else:
                action_summary = "Ready to continue"
                action_message = "All changes staged. Click Continue Merge to complete."
        elif staged_count > 0:
            action_summary = "Ready to commit"
            action_message = f"{staged_count} file(s) staged for commit."
        elif unstaged_count > 0:
            action_summary = "Pending changes"
            action_message = f"{unstaged_count} unstaged change(s)."
        #elif summary["untracked"]:
        #    action_summary = "Untracked files"
        #    action_message = f"{len(summary['untracked'])} untracked file(s)."
        else:
            action_summary = "Up to date"
            action_message = "Working tree clean."

        status_dict = {
            "summary": summary,
            "action_summary": action_summary,
            "action_message": action_message,
            "merge_in_progress": merge_in_progress,
            "branch": branch_info or "HEAD"
        }
        logger.debug(f"Git status raw output {status_dict} chars)")
        return status_dict, None

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() or str(e)
        logger.error(f"Git status failed for {repo_path}: {error_msg}")
        return None, {"type": "GitError", "message": error_msg}
    except Exception as e:
        logger.error(f"Unexpected error in get_git_status_data({repo_path}): {str(e)}")
        return None, {"type": "Exception", "message": str(e)}
    # fallback just in case
    logger.error("Unknown error in get_git_status_data for %s", repo_path)
    return None, {"type": "Exception", "message": "Unknown error"}

@app.route('/api/status', methods=['POST'])
def get_status():
    logger.info("API endpoint /api/status called")
    check_api_key()

    data = request.json
    repo_path = data.get('repo_path')
    if not repo_path:
        return jsonify({"error": "Repository path is required"}), 400

    status_data, status_error = get_git_status_data(repo_path)
    if status_error:
        logger.error("Error retrieving git status for %s: %s", repo_path, status_error["message"])
        return jsonify({
            "error": status_error["message"],
            "type": status_error["type"]
        }), 500

    status_data["message"] = "Git status retrieved successfully."
    return jsonify(status_data), 200

#@app.route('/api/pull', methods=['POST'])
#def pull():
#    """
#    API to pull changes from remote
#    """
#    check_api_key()
#    try:
#        origin = repo.remotes.origin
#        pull_info = origin.pull()
#        return jsonify({"status": "Pull successful", "info": str(pull_info)}), 200
#    except Exception as e:
#        return jsonify({"error": str(e)}), 500

@app.route('/api/pull/object', methods=['GET'])
def pull_object():
    logger.info("API endpoint /api/pull/object called")
    # Verify API key
    check_api_key()
    # Get the repository path from headers
    repo_path = request.headers.get('Repo-Path')
    logger.debug("Received repo_path: %s", repo_path)

    if not repo_path:
        logger.error("'repo_path' is required")
        return jsonify({"error": "Repository path is required"}), 400

    # Validate repository
    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Verify session ID
    #session_id = request.headers.get('Session-ID')
    #if not session_id:
    #    return jsonify({"error": "Session ID is required"}), 400

    # Define lock path and validate the session
    #local_lock_path = os.path.join(repo_path, '.git/refs/heads/pull.lock')
    #if not lock_exists(local_lock_path) or not validate_lock(local_lock_path, session_id):
    #    return jsonify({"error": "Invalid or missing session lock for this repository"}), 403

    # Get the file path from the header
    file_path = request.headers.get('File-Path')
    if not file_path:
        logger.error("File path is required")
        return jsonify({"error": "File path is required"}), 400

    object_file_path = os.path.join(repo_path, '.git/objects', file_path)
    logger.debug("Resolved object_file_path: %s", object_file_path)
    if not os.path.isfile(object_file_path):
        logger.error("The file '%s' does not exist on the server", file_path)   
        return jsonify({"error": f"The file '{file_path}' does not exist on the server"}), 404

    # Parse the Range header for partial content requests
    range_header = request.headers.get('Range')
    if not range_header:
        logger.error("Range header is required")
        return jsonify({"error": "Range header is required"}), 400

    # Extract start and end bytes from the Range header
    try:
        range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not range_match:
            return jsonify({"error": "Invalid Range header format"}), 400

        start_byte = int(range_match.group(1))
        end_byte = int(range_match.group(2)) if range_match.group(2) else None

    except ValueError:
        return jsonify({"error": "Invalid byte range specified"}), 400

    # Get the file size and adjust end_byte if it's None
    file_size = os.path.getsize(object_file_path)
    if end_byte is None or end_byte >= file_size:
        end_byte = file_size - 1

    if start_byte >= file_size:
        return jsonify({"error": "Requested range is not satisfiable"}), 416

    # Calculate the number of bytes to read
    chunk_size = end_byte - start_byte + 1

    # Open file and read the requested byte range
    try:
        with open(object_file_path, 'rb') as file:
            file.seek(start_byte)
            data = file.read(chunk_size)

        # Set up response with partial content
        response = make_response(data)
        response.status_code = 206  # HTTP 206 Partial Content
        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Range'] = f"bytes {start_byte}-{end_byte}/{file_size}"
        response.headers['Content-Length'] = str(chunk_size)

        logger.debug("Serving bytes %d-%d of '%s'", start_byte, end_byte, file_path)
        return response

    except Exception as e:
        logger.error("Error reading object file: %s", str(e))
        return jsonify({"error": "Failed to read the object file"}), 500

@app.route('/api/local-path', methods=['POST'])
def repo_path():
    """
    API to pull changes from remote
    """
    logger.info("API endpoint /api/local-path called")
    check_api_key()
    try:
        return jsonify({"status": "success", "local_path": str(REPO_PATH)}), 200
    except Exception as e:
        logger.error("Error retrieving local path: %s", str(e))
        return jsonify({"error": str(e)}), 500

def extract_branch_from_error(error_message):
    # Regular expression to extract the branch name inside the single quotes

    match = re.search(r"your current branch '(.+?)'", error_message)
    if match:
        return match.group(1)
    return None

def get_current_branch_or_default(repo_path):
    """
    Returns the current branch name if the repository has commits.
    Otherwise, returns the default branch name to be used after the first commit.
    """
    logger.debug("Getting current branch or default for repo_path: %s", repo_path)
    # First, check if there are any commits in the repository
    log_output = run_git_command(repo_path, [GIT_EXECUTABLE, "log", "-1"])  # This will throw an error if there are no commits
    logger.debug("git log output: %s", log_output)

    if "does not have any commits yet" in log_output:
        # Handle the case where the branch has no commits
        logger.info("processing first commit...")
        branch = extract_branch_from_error(log_output)
        logger.debug("Found branch: %s", branch)
        
        return {
            'status_message': f"Branch '{branch}' has no commits yet.",
            'branch': branch
            }
        #return f"Branch '{branch}' has no commits yet."

    # If commits exist, get the current branch name
    branch = run_git_command(repo_path, [GIT_EXECUTABLE, "rev-parse", "--abbrev-ref", "HEAD"])

    return {'branch':branch.strip(), 'status_message':''}

@app.route('/user/repos', methods=['POST'])
def create_user_repos():
    logger.info("API endpoint /user/repos called")
    check_api_key()
    # Get JSON payload
    data = request.get_json()

    # Validate the incoming JSON data
    if not data or 'name' not in data:
        logger.error("Invalid input! Name is required.")
        return jsonify({'message': 'Invalid input! Name is required.'}), 400

    repo_name = data['name']

    # Create the directory for the new repository
    repo_path = os.path.join(REPOS_BASE_PATH, repo_name)

    try:
        # Initialize a new Git repository
        subprocess.run(['git', 'init', repo_path], check=True)
        

        ### Create a README file with the description
        #with open(os.path.join(repo_path, 'README.md'), 'w') as readme_file:
        #    readme_file.write(f"# {repo_name}\n\n{repo_description}")

        logger.debug("New repository created at %s", repo_path)
        return jsonify({'message': 'Repository created successfully!', 'name': repo_name}), 201

    except subprocess.CalledProcessError:
        logger.error("Failed to create the repository")
        return jsonify({'message': 'Failed to create the repository!'}), 500

    except Exception as e:
        logger.error("Exception during repository creation: %s", str(e))
        return jsonify({'message': str(e)}), 500

@app.route('/user/repos', methods=['GET'])
def list_user_repos():
    logger.info("API endpoint /user/repos called")
    check_api_key()
    auth_token = request.headers.get('Authorization')
    logger.debug("Authorization token: %s", auth_token)
    repositories = []
    index = 0

    for repo_path in REPOSITORIES:
        # Get the repository name and local path
        repo_name = os.path.basename(repo_path)
        local_path = repo_path

        # Get the current branch
        branch = get_current_branch_or_default(local_path)

        # Get the status of the repository
        status = run_git_command(local_path, [GIT_EXECUTABLE, "status"])

        # Determine action status based on the status output
        action_status = "already up to date"  # Default status
        if "Changes to be committed" in status:
            action_status = "need to commit"
        elif "Changes not staged for commit" in status:
            action_status = "need to commit"
        elif "Untracked files" in status:
            action_status = "need to commit"
        elif "Your branch is ahead" in status:
            action_status = "ready to push"
        elif "Your branch is behind" in status:
            action_status = "ready to pull"
        elif "You have unmerged paths" in status:
            action_status = "merge"

        # Get the remote URL
        remote = run_git_command(local_path, [GIT_EXECUTABLE, "remote", "-v"])

        # Parse the remote information to get the URL
        remote_url = None
        if remote:
            remote_lines = remote.splitlines()
            remote_url = remote_lines[0].split()[1] if remote_lines else None

        # Append the repository information to the list
        repositories.append({
            "name": repo_name,
            "id": index,
            "local_path": local_path,
            "status": status,
            "action_status": action_status,
            "branch": branch['branch'],
            "status_message": branch['status_message'],
            "remote": remote_url,
            "description": "Stargit Repository"
        })

        index += 1

    # Construct the response
    response = {
        "repositories": repositories
    }

    return jsonify(repositories), 200

@app.route('/api/repositories', methods=['GET'])
def list_repositories():
    """
    API to list repositories with their name, local path, status, branch, and remote URL.
    """
    logger.info("API endpoint /api/repositories called")
    check_api_key()
    try:
        repositories = []
        for repo_path in REPOSITORIES:
            logger.debug("Checking repository at path: %s", repo_path)
            # Get the repository name and local path
            repo_name = os.path.basename(repo_path)
            local_path = repo_path

            # Get the current branch
            branch = get_current_branch_or_default(local_path)

            # Get the status of the repository
            status = run_git_command(local_path, [GIT_EXECUTABLE, "status"])

            # Determine action status based on the status output
            action_status = "already up to date"  # Default status
            if "Changes to be committed" in status:
                action_status = "need to commit"
            elif "Changes not staged for commit" in status:
                action_status = "need to commit"
            elif "Untracked files" in status:
                action_status = "need to commit"
            elif "Your branch is ahead" in status:
                action_status = "ready to push"
            elif "Your branch is behind" in status:
                action_status = "ready to pull"
            elif "You have unmerged paths" in status:
                action_status = "merge"

            # Get the remote URL
            remote = run_git_command(local_path, [GIT_EXECUTABLE, "remote", "-v"])

            # Parse the remote information to get the URL
            remote_url = None
            remote_name = None
            if remote:
                remote_lines = remote.splitlines()
                remote_url = remote_lines[0].split()[1] if remote_lines else None
                remote_name = remote_lines[0].split()[0] if remote_lines else None

            # Append the repository information to the list
            repositories.append({
                "name": repo_name,
                "local_path": local_path,
                "status": status,
                "action_status": action_status,
                "branch": branch['branch'],
                "status_message": branch['status_message'],
                "remote": remote_url,
                "remote_name": remote_name
            })

        # Construct the response
        response = {
            "repositories": repositories
        }

        return jsonify(response), 200

    except Exception as e:
        logger.error("Error in /api/repositories: %s", str(e))
        print("An exception occured", str(e), flush=True)
        return jsonify({"error": str(e)}), 500


def list_directory_content(path, options, exclusion_patterns=None):
    result = []
    index = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            full_path = os.path.join(root, name)
            relative_path = os.path.relpath(full_path, path)

            # Check if the file matches any of the exclusion patterns
            if exclusion_patterns:
                for pattern in exclusion_patterns:
                    if pattern in name:
                        logger.debug(f"Excluding file based on pattern '{pattern}': {relative_path}")   
                        continue

            file_size = os.path.getsize(full_path)
            file_attributes = os.stat(full_path)

            file = {}

            if options.get("index", True):
                file['index'] = len(result)

            if options.get("name", True):
                file['name'] = name

            if options.get("relative_path", True):
                file['relative_path'] = relative_path

            if options.get("size", True):
                file['size'] = file_size

             # Check for 'attributes' key and its value in options
            if options.get("attributes", True):
                file['attributes'] = {
                    'mode': file_attributes.st_mode,
                    'mtime': file_attributes.st_mtime,
                    'atime': file_attributes.st_atime,
                    'ctime': file_attributes.st_ctime
                }


            result.append(file)
    return result

@app.route('/api/object/info', methods=['POST'])
def get_object_info():
    logger.info("API endpoint /api/object/info called")
    check_api_key()

    data = request.json
    repo_path = data.get('repo_path')
    object_file = data.get('object_file')

    # Build the full path to the object file
    file_path = os.path.join(repo_path, ".git", "objects", object_file)

    # Initialize a dictionary to store file information
    file_info = {}

    try:
        # Get file info if it exists
        if os.path.isfile(file_path):
            file_info = {
                "file_name": os.path.basename(file_path),
                "size_in_bytes": os.path.getsize(file_path),
                "file_path": file_path
            }
        else:
            return jsonify({"error": "File does not exist"}), 404
    except Exception as e:
        print("error exceptions", str(e))
        return jsonify({"error": str(e)}), 500

    return jsonify(file_info), 200

@app.route('/api/objects', methods=['POST'])
def list_object_files():
    logger.info("API endpoint /api/objects called")
    check_api_key()
    data = request.json
    repo_path = data.get('repo_path')

    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    objects_path = repo_path+'/.git/objects'  # Update this to point to your repository's .git/objects
    objects = list_directory_content(objects_path, data)
    return jsonify(objects)

@app.route('/api/refs', methods=['POST'])
def list_git_refs():
    logger.info("API endpoint /api/refs called")
    check_api_key()

    data = request.json
    repo_path = data.get('repo_path')
    print("repo_path", repo_path, flush=True)
    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    refs_path = repo_path+'/.git/refs'  # Update this to point to your repository's .git/refs
    refs = list_directory_content(refs_path, data, ['lock'])  # Pass 'lock' as the exclusion pattern for refs
    return jsonify(refs)

####################################
#
# DOWNLOAD API for incremental push and clone
#

def download_file(data, sub_path):
    repo_path = data.get('repo_path')
    relative_path  = data.get('relative_path')
    file_offset = data.get('file_offset', 0)
    max_bytes = data.get('max_bytes')  # max_bytes can be None if not provided

    # Validate repository
    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Construct the path to the object or ref file
    object_path = os.path.join(repo_path, '.git', sub_path, relative_path)  # Include relative path

    logger.debug("Downloading file from path: %s", object_path)

    # Read the specified object file
    if not os.path.isfile(object_path):
        logger.error("File not found at path: %s", object_path)
        return jsonify({"error": f"File not found at '{object_path}'"}), 404

    try:
        with open(object_path, 'rb') as file:
            file.seek(file_offset)  # Move the file pointer to the specified offset

            # Read the specified number of bytes or the entire file if max_bytes is None
            data = file.read(max_bytes) if max_bytes is not None else file.read()

            if not data:
                logger.error("No data read from the specified offset")
                return jsonify({"error": "No data read from the specified offset"}), 404

            # Return the raw bytes and the size
            response = Response(data, mimetype='application/octet-stream')
            response.headers['Content-Length'] = str(len(data))
            return response
    except Exception as e:
        logger.error("Error reading file: %s", str(e))
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/object', methods=['POST'])
def download_object():
    logger.info("API endpoint /api/download/object called")
    check_api_key()
    return download_file(request.json, 'objects')

@app.route('/api/download/ref', methods=['POST'])
def download_ref():
    logger.info("API endpoint /api/download/ref called")
    check_api_key()
    return download_file(request.json, 'refs')

####################################
#
# PUSH API
#

#
# push api helper functions
#

# Helper function to check if a lock exists
def lock_exists(lock_path):
    return os.path.exists(lock_path)

# Helper function to create a lock file with JSON content
def create_lock(lock_path, lock_content):
    # Write the lock content to a .lock file
    try:
        with open(lock_path, 'w') as lock_file:
            json.dump(lock_content, lock_file)
        logger.debug("Created lock for push at %s", lock_path)
        return True
    except Exception as e:
        logger.error("Failed to create lock for push at %s: %s", lock_path, str(e)) 
        return False

def get_commit_hash(repo_path, ref):
    """Get the commit hash from the specified reference."""
    ref_path = os.path.join(repo_path, ref)
    if os.path.exists(ref_path):
        with open(ref_path, 'r') as f:
            return f.read().strip()  # Return the commit hash as a string
    return None

# API to initiate push and create locks
@app.route('/api/push/start', methods=['POST'])
def initiate_push():
    logger.info("API endpoint /api/push/start called")
    check_api_key()
    data = request.json
    repo_path = data.get('repo_path')
    branch = data.get('branch')
    logger.debug("Push start for repo_path: %s, branch: %s", repo_path, branch)
    
    client_id = data.get('client_id')  # Client ID or generate a random one
    logger.debug("Received client_id: %s", client_id)
    session_id = str(uuid.uuid4())  # Generate a unique session ID
    logger.debug("Generated session_id: %s", session_id)

    # Get the client head commit and remote commit
    client_head_commit = data.get('head_commit')
    client_remote_commit = data.get('remote_commit')

    logger.debug("Received client_head_commit: %s", client_head_commit)
    logger.debug("Received client_remote_commit: %s", client_remote_commit)

    lock_content = {
        "creation_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
        "last_update": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
        "client_id": client_id,
        "session_id": session_id
    }

    if client_head_commit:
        lock_content['client_head_commit'] = client_head_commit
    if client_remote_commit:
        lock_content['client_remote_commit'] = client_remote_commit

    # Get the current branch HEAD commit
    local_head_commit = get_commit_hash(repo_path, f'.git/refs/heads/{branch}')
    new_branch = False
    logger.debug("Local head commit for branch '%s': %s", branch, local_head_commit)
    if local_head_commit is None:
        logger.debug("Local branch '%s' does not exist, creating new branch.", branch)
    
    # Verify that the local head commit and client remote commit are the same
    elif client_remote_commit != local_head_commit:
        logger.error("Local branch is out of date. Local head: %s, Client remote: %s", local_head_commit, client_remote_commit)
        return jsonify({"error": "Your local branch is out of date. Please pull the latest changes before pushing."}), 409
    else:
        lock_content['local_head_commit'] = local_head_commit

    # Validate the repository
    if repo_path not in REPOSITORIES:
        logger.error("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository '{repo_name}' not found"}), 400

    # Define lock paths
    local_lock_path = os.path.join(repo_path, f'.git/refs/heads/{branch}.lock')
    remote_lock_path = os.path.join(repo_path, '.git/refs/remotes/push.lock')

    # Check for existing locks
    if lock_exists(local_lock_path) or lock_exists(remote_lock_path):
        logger.error("A push is already in progress. Lock file exists.")
        return jsonify({"error": "A push is already in progress. Lock file exists."}), 409

    # Create local lock (if local refs exist)
    local_heads_path = os.path.join(repo_path, '.git/refs/heads')
    # Create the directory if it does not exist
    #os.makedirs(local_heads_path, exist_ok=True)
    if os.path.exists(local_heads_path):
        lock_content['local_heads_path'] = local_heads_path
        if not create_lock(local_lock_path, lock_content):
            logger.error("Failed to create local lock at %s", local_lock_path)
            return jsonify({"error": "Failed to create local lock"}), 500

    # Create remote lock (if remote refs exist)
    remote_heads_path = os.path.join(repo_path, '.git/refs/remotes')
    os.makedirs(remote_heads_path, exist_ok=True)
    if os.path.exists(remote_heads_path):
        lock_content['remote_heads_path'] = remote_heads_path
        if not create_lock(remote_lock_path, lock_content):
            logger.error("Failed to create remote lock at %s", remote_lock_path)
            return jsonify({"error": "Failed to create remote lock"}), 500

    logger.debug("Initiated push with session id: %s", session_id)
    # Return session ID to be used for future push operations
    return jsonify({
        "message": "Push initiated successfully",
        "session_id": session_id
    }), 200

# Helper function to validate the session ID in the lock file
def validate_lock(lock_path, session_id):
    try:
        with open(lock_path, 'r') as lock_file:
            lock_content = json.load(lock_file)
            return lock_content.get('session_id') == session_id
    except Exception as e:
        logger.error("Failed to validate lock at %s: %s", lock_path, str(e))
        return False

# Helper function to remove the lock file
def remove_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
            logger.debug("Removed lock at %s", lock_path)
            return True
        return False
    except Exception as e:
        logger.error("Failed to remove lock at %s: %s", lock_path, str(e))
        return False

def is_bare_repository(repo_path):
    """Check if the repository is a bare repository."""
    return repo_path.endswith('.git') and os.path.isdir(repo_path)


def reset_branch(repo_path, branch):
    """
    Check if the repository is bare or non-bare, and if non-bare, perform `git reset --hard`
    if the branch being pushed matches the currently checked-out branch.

    Returns a JSON response indicating success or failure.
    """
    logger.debug("Resetting branch '%s' in repository at path: %s", branch, repo_path)
    # Check if the repository is bare or non-bare

    if is_bare_repository(repo_path):
        logger.debug("Repository is bare, no reset needed.")
        return {"message": "No need to reset bare repository."}, 200

    git_dir = os.path.join(repo_path, ".git")

    # If non-bare, check if the branch is the current branch in the working directory
    head_file_path = os.path.join(git_dir, "HEAD")
    if not os.path.exists(head_file_path):
        logger.error("HEAD file not found in non-bare repository at %s", git_dir)
        return {"error": "HEAD file not found, unable to reset"}, 404

    # Determine the current branch (if not bare)
    with open(head_file_path, "r") as f:
        head_content = f.read().strip()

    # Example format of HEAD: "ref: refs/heads/master"
    if head_content.startswith("ref: "):
        current_branch = head_content.split("/")[-1]  # Extract branch name from HEAD
        logger.debug("Current branch: %s", current_branch)
    else:
        current_branch = None
        logger.debug(f"HEAD is detached, current commit is {head_content}")

    # Only reset if this is a non-bare repo and the branch matches the current branch
    if current_branch and branch == current_branch:
        try:
            logger.debug("Resetting branch '%s' in non-bare repository at %s to match the latest pushed commit.", branch, repo_path)
            
            # Perform git reset --hard using the git binary
            result = subprocess.run(['git', 'reset', '--hard'], cwd=repo_path, capture_output=True, text=True)

            if result.returncode != 0:
                logger.error(f"Error during git reset --hard: {result.stderr}")
                return {"error": f"Failed to reset branch: {result.stderr}"}, 500

            logger.debug(f"Branch {branch} successfully reset --hard.")
            return {"message": f"Push completed. Branch {branch} reset --hard to the latest commit."}, 200

        except Exception as e:
            logger.error(f"Exception during git reset --hard: {str(e)}")
            return {"error": f"Failed to reset branch: {str(e)}"}, 500

    else:
        # No reset needed (bare repo or branch does not match current branch)
        logger.debug(f"No reset needed (bare repo or branch {branch} not checked out).")
        return {"message": "Push No reset performed (requested branch {branch} not checked out)."}, 200


# API to stop push and remove locks
@app.route('/api/push/end', methods=['POST'])
def stop_push():
    logger.info("API endpoint /api/push/end called")
    check_api_key()
    data = request.json
    repo_path = data.get('repo_path')
    session_id = data.get('session_id')  # Session ID to validate
    branch = data.get('branch')
    logger.debug("Processing push end for repo_path: %s, branch: %s, session_id: %s", repo_path, branch, session_id)

    # Validate the repository
    if repo_path not in REPOSITORIES:
        logger.warning("Repository '%s' not found", repo_path)
        return jsonify({"error": f"Repository '{repo_path}' not found"}), 400

    # Define lock paths
    local_lock_path = os.path.join(repo_path, f'.git/refs/heads/{branch}.lock')
    remote_lock_path = os.path.join(repo_path, '.git/refs/remotes/push.lock')

    # Validate and remove the local lock if it exists

    if lock_exists(local_lock_path):
        if not validate_lock(local_lock_path, session_id):
            logger.warning("Invalid session for local lock: %s", error_message)
            return jsonify({"error": "Invalid session ID for local lock"}), 403

    # Validate and remove the remote lock if it exists
    if lock_exists(remote_lock_path):
        if not validate_lock(remote_lock_path, session_id):
            logger.warning("Invalid session for remote lock: %s", error_message)
            return jsonify({"error": "Invalid session ID for remote lock"}), 403

    
    workdir_branch = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "branch", "--show-current"])
    logger.debug("Current workdir branch: %s", workdir_branch)

    # Define the paths
    head_path = os.path.join(repo_path, '.git', 'HEAD')

    # Check if .git/HEAD exists
    if not os.path.exists(head_path):
        # Create .git/HEAD and write the reference to the specified branch
        logger.info("Creating .git/HEAD for branch: %s", branch)
        branch_ref = f"ref: refs/heads/{branch}\n"
        with open(head_path, 'w') as head_file:
            head_file.write(branch_ref)
        logger.info("Created .git/HEAD for branch: %s", branch)
    else:
        logger.info(".git/HEAD already exists")

    if lock_exists(local_lock_path):
        if not remove_lock(local_lock_path):
            logger.error(f"error: Failed to remove local lock file {local_lock_path}")
            return jsonify({"error": "Failed to remove local lock"}), 500

@app.route('/api/push/object', methods=['POST'])
def push_object():
    logger.info("API endpoint /api/push/object called")
    # Verify API key
    check_api_key()
    repo_path = request.headers.get('Repo-Path')
    logger.debug("Processing push object for repo_path: %s", repo_path)

    # Validate repository
    if repo_path not in REPOSITORIES:
        logger.warning("Repository path '%s' not found in registered repositories", repo_path)
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Verify session ID
    session_id = request.headers.get('Session-ID')
    print("session_id", session_id, flush=True)
    if not session_id:
        logger.warning("Session ID is required")
        return jsonify({"error": "Session ID is required"}), 400

    # Define lock paths
    local_lock_path = os.path.join(repo_path, '.git/refs/remotes/push.lock')

    # Check for existing locks
    if not lock_exists(local_lock_path):
        # Return error message if the lock for a valid push session is missing
        logger.warning("Missing lock for valid push session on %s", local_lock_path)
        return jsonify({"error": f"Missing lock for valid push session on {local_lock_path}."}), 404

    if not validate_lock(local_lock_path, session_id):
        logger.warning("Invalid session ID %s for local lock", session_id)
        return jsonify({"error": "Invalid session ID for local lock"}), 403
    
    # Get the file path from the header
    file_path = request.headers.get('File-Path')
    
    if not file_path:
        logger.warning("File path is required")
        return jsonify({"error": "File path is required"}), 400
    
    # Get the upload mode from the header
    upload_mode = request.headers.get('Upload-Mode', 'full')  # Default to 'full' if not provided
    logger.debug("Upload mode: %s", upload_mode)

    # Read the binary data from the request
    binary_data = request.data

    # Save the object to a file
    object_file_path = os.path.join(repo_path, '.git/objects', file_path)
    logger.debug("Receiving file path: %s", object_file_path)

    try:
        # Create the directory if it doesn't exist
        os.makedirs(os.path.dirname(object_file_path), exist_ok=True)

        # Determine write mode based on upload mode
        if upload_mode == 'start':
            # Create or overwrite the file for the initial chunk
            with open(object_file_path, 'wb') as obj_file:
                obj_file.write(binary_data)
            logger.info("File started: %s", object_file_path)

        elif upload_mode == 'append':
            # Append to the file for subsequent chunks
            with open(object_file_path, 'ab') as obj_file:
                obj_file.write(binary_data)
            logger.info("Data appended to: %s", object_file_path)

        elif upload_mode == 'full':
            # Write the full file in one go
            with open(object_file_path, 'wb') as obj_file:
                obj_file.write(binary_data)
            logger.info("Full file uploaded: %s", object_file_path)

        else:
            logger.warning("Invalid upload mode specified: %s", upload_mode)
            return jsonify({"error": "Invalid upload mode specified"}), 400

        logger.info("Object pushed successfully: %s", object_file_path)
        return jsonify({"message": "Object pushed successfully", "file_path": object_file_path}), 200

    except Exception as e:
        logger.error("Failed to save object %s: %s", object_file_path, str(e))
        return jsonify({"error": "Failed to save the object"}), 500

@app.route('/api/push/ref', methods=['POST'])
def update_ref():
    logger.info("API endpoint /api/push/ref called")
    check_api_key()
    # Get data from request
    data = request.json
    commit_id = data.get('commit_id')  # The commit ID to set
    repo_path = data.get('repo_path')   # The repository path
    session_id = data.get('session_id')  # Validate the session ID
    branch = data.get('branch')
    logger.debug("Updating ref for repo_path: %s, branch: %s, commit_id: %s", repo_path, branch, commit_id)

    # Validate session
    # Define lock paths
    local_lock_path = os.path.join(repo_path, '.git/refs/remotes/push.lock')
    is_valid, error_message = validate_session(local_lock_path, session_id)
    if not is_valid:
        logger.warning("Invalid session: %s", error_message)
        return jsonify({"error": error_message}), 403 if "Invalid session" in error_message else 404

    # Define the path to heads/master
    heads_master_path = os.path.join(repo_path, f'.git/refs/heads/{branch}')
    logger.debug("Heads master path: %s", heads_master_path)

    # Extract the directory portion of the path (exclude the file)
    directory = os.path.dirname(heads_master_path)
    
    # Check if the directory exists, and if not, create it
    if not os.path.exists(directory):
        logger.info("Creating branch ref directory: %s", directory)
        os.makedirs(directory)  # This will create all necessary parent di

    # Update heads/master with the new commit ID
    current_commit_id = None
    try:
        # Read the current commit ID from heads/{branch}
        if os.path.exists(heads_master_path):
            logger.debug("Reading heads/%s", branch)
            with open(heads_master_path, 'r') as f:
                current_commit_id = f.read().strip()

            # Check if the current commit ID is already up to date
            if current_commit_id == commit_id:
                logger.info("%s is already up to date with commit %s", branch, commit_id)
                return jsonify({"message": f"{branch} is already up to date", "commit_id": commit_id}), 200

        logger.debug("Writing to heads/%s with commit_id: %s", branch, commit_id)
        with open(heads_master_path, 'w') as f:
            f.write(commit_id + '\n')
        
        result = {
            "message": f"Reference updated successfully for {branch}",
            "from_commit_id": current_commit_id,
            "to_commit_id": commit_id
        }
        if current_commit_id:
            logger.info("Updated %s from %s to %s", branch, current_commit_id, commit_id)
            result["message"] = f"Reference updated successfully for {branch} from {current_commit_id} to {commit_id}"
        else:
            logger.info("Created new branch ref %s set to commit %s", branch, commit_id)
            result["message"] = f"Created new branch successfully for {branch} set to {commit_id}"

        return jsonify(result), 200

    except Exception as e:
        logger.error("Failed to update reference for %s: %s", branch, str(e))
        return jsonify({"error": "Failed to update reference"}), 500

def collect_server_metrics():
    """Collect basic server metrics for registration."""
    try:
        uptime = time.time() - psutil.boot_time()
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        host_name = socket.gethostname()
        stats = {
            'host_name': host_name,
            "uptime_seconds": uptime,
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "repo_count": len(REPOSITORIES),
            "version": "1.0.0"  # Replace with __version__ or similar
        }
        print("Collected server metrics:", stats, flush=True)
        return stats
    except Exception as e:
        logger.error("Failed to collect server metrics: %s", str(e))
        return {}

def get_access_token():
    """Fetch or refresh an access token."""
    global tokens
    verbose = False
    if tokens['access_token'] and tokens['expires_at'] and datetime.utcnow() < tokens['expires_at'] - timedelta(seconds=60):
        if verbose:
            logger.debug("Using existing valid access token")
        return tokens['access_token']

    # Try refreshing if refresh_token exists
    if tokens['refresh_token']:
        try:
            response = requests.post(
                AUTH_ENDPOINT.replace('/token', '/refresh'),
                json={'refresh_token': tokens['refresh_token']}
            )
            if response.status_code == 200:
                data = response.json()
                tokens['access_token'] = data['access_token']
                tokens['expires_at'] = datetime.utcnow() + timedelta(hours=1)
                logger.info("Access token refreshed successfully")
                return tokens['access_token']
            else:
                logger.warning("Failed to refresh token from %s: %s (status: %d)", AUTH_ENDPOINT.replace('/token', '/refresh'), response.text, response.status_code)
        except Exception as e:
            logger.warning("Failed to refresh token: %s", str(e))

    # Fetch new token
    if not STARGIT_API_KEY:
        logger.error("STARGIT_API_KEY not set in .env")
        return None
    
    if not SERVER_UUID:
        logger.error("STARBRIDGE_SERVER_UUID not found in .env — please run setup script first.")
        return None

    logger.debug("Requesting new token using API key %s... for server %s", STARGIT_API_KEY[:8], SERVER_UUID)

    try:
        headers = {
            "Authorization": f"Bearer {STARGIT_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "scopes": "servers:register servers:heartbeat servers:poll servers:live-update",
            "metadata": {  # include server context here
                "server_uuid": SERVER_UUID
            }
        }
        response = requests.post(AUTH_ENDPOINT, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            tokens['access_token'] = data['access_token']
            tokens['refresh_token'] = data.get('refresh_token')
            tokens['expires_at'] = datetime.utcnow() + timedelta(hours=1)
            tokens['api_key_uuid'] = data.get('api_key_uuid') # optional: still store for reference
            logger.info("New access token fetched successfully for server_uuid: %s", SERVER_UUID)
            return tokens['access_token']
        else:
            logger.error("Failed to fetch token from %s: %s (status: %d, key: %s...)", AUTH_ENDPOINT, response.text, response.status_code, STARGIT_API_KEY[:8])
            return None
    except Exception as e:
        logger.error("Error fetching token from %s: %s (key: %s...)", AUTH_ENDPOINT, str(e), STARGIT_API_KEY[:8])
        return None


import concurrent.futures

def get_file_list(repo_path):
    """Get a list of committed files with metadata: name, latest_sha, size."""
    # Single command for all files: mode type blob_sha size path
    command = [GIT_EXECUTABLE, "-C", repo_path, "ls-tree", "-r", "-l", "HEAD"]
    output = run_git_command(repo_path, command)
    if output.startswith("Error:"):
        logger.warning("Failed to get file list for repo %s", repo_path)
        return [], 0
    
    file_info = []
    total_size = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=4)  # mode, type, blob_sha, size, path (handle spaces in paths)
        if len(parts) == 5:
            mode, obj_type, blob_sha, size_str, file_path = parts
            if obj_type == 'blob':  # Only files, skip trees
                file_size = int(size_str) if size_str.isdigit() else 0
                total_size += file_size
                file_info.append({
                    "name": file_path.strip(),
                    "blob_sha": blob_sha,  # Not latest commit SHA, but blob SHA (if needed)
                    "size": file_size
                })
    
    # Parallelize latest commit SHA per file
    def get_latest_sha(file_path):
        command_sha = [GIT_EXECUTABLE, "-C", repo_path, "log", "-1", "--format=%H", "--", file_path]
        latest_sha = run_git_command(repo_path, command_sha)
        return latest_sha if not latest_sha.startswith("Error:") else None
    
    file_paths = [f["name"] for f in file_info]
    with concurrent.futures.ThreadPoolExecutor() as executor:
        shas = list(executor.map(get_latest_sha, file_paths))
    
    for info, sha in zip(file_info, shas):
        info["latest_sha"] = sha
    
    return file_info, total_size

def get_readme_text(repo_path):
    """Read and serialize README.md text if it exists."""
    readme_path = os.path.join(repo_path, "README.md")
    if os.path.exists(readme_path):
        try:
            with open(readme_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error("Error reading README.md in %s: %s", repo_path, str(e))
            return ""
    #logger.debug("No README.md found in %s", repo_path)
    return ""

# Updated collect_repo_details
def collect_repo_details():
    """
    Collect detailed information about all local repositories, including branches, status, remotes, and commits.
    Returns a structure similar to the provided example.
    """
    servers = {"name": "Origin Server", "repos": []}
    
    for repo_path in REPOSITORIES:
        repo_name = os.path.basename(repo_path)
        repo = {"name": repo_name}
        
        # Get branch info
        branch_info = get_current_branch_or_default(repo_path)
        repo["branch"] = branch_info['branch']
        
        # Get status and determine action status
        status_data, status_error = get_git_status_data(repo_path)
        if status_error:
            repo["status"] = {"error": status_error["message"]}
        else:
            repo["status"] = status_data
        
        remote_heads = git_utils.get_remote_heads(repo_path)
        repo["remote_heads"] = remote_heads

        # Get remotes
        remotes_info = []
        remotes_output = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "remote", "-v"])
        seen = set()  # To avoid duplicates if fetch/push are the same
        for line in remotes_output.splitlines():
            if line.strip():
                parts = line.split()
                if len(parts) >= 3:
                    name, url, typ = parts[0], parts[1], parts[2].strip('()')
                    key = (name, typ, url)
                    if key not in seen:
                        remotes_info.append({"name": name, "type": typ, "url": url})
                        seen.add(key)
        repo["remotes"] = remotes_info
        print("repo['remotes']", repo["remotes"], flush=True)
        
        branches, branches_error = get_branches_data(repo_path)
        if branches_error:
            print("Failed to get branches:", branches_error)
            repo["branches"] = branches_error
        else:
            print("Branches data:", branches)
            repo["branches"] = branches
        
        # Get commits (using rev_walk logic)
        command = [GIT_EXECUTABLE, "-C", repo_path, "log", "--date=iso", "--pretty=format:%H|%P|%an|%ae|%ad|%s", repo["branch"]]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        commits = []
        if process.returncode == 0:
            commit_lines = stdout.decode('utf-8').splitlines()
            for line in commit_lines:
                if line.strip():
                    try:
                        sha, parents, author_name, author_email, date, message = line.split("|", 5)
                        parents_list = parents.split() if parents else []
                        commits.append({
                            "sha": sha,
                            "parents": parents_list,
                            "author_name": author_name,
                            "author_email": author_email,
                            "date": date,
                            "message": message
                        })
                    except ValueError:
                        logger.warning("Malformed commit line in repo %s: %s", repo_name, line)
        else:
            logger.warning("Failed to fetch commits for repo %s: %s", repo_name, stderr.decode('utf-8'))
        repo["commits"] = commits
        
        # Add files list and total size
        files, total_size = get_file_list(repo_path)
        repo["files"] = files
        repo["storage_size"] = total_size
        
        # Add README text
        repo["readme"] = get_readme_text(repo_path)
        
        servers["repos"].append(repo)
    
    return servers

def safe_rev_parse(repo_path, ref):
    """Safely resolve a ref, even during rebase or detached HEAD"""
    result = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "rev-parse", "--verify", ref])
    if result.startswith("Error:") or not result.strip():
        # Fallback: try to resolve via HEAD if ref is current
        current = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "symbolic-ref", "-q", "HEAD"])
        if current and current.strip().endswith(ref):
            return run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "rev-parse", "HEAD"])
        return None
    return result.strip()


# Collect lightweight summaries DEPRECATED
def collect_repo_summaries(include_remote=True):
    summaries = {}
    
    for repo_path in REPOSITORIES:
        repo_name = os.path.basename(repo_path)

        print(f"[collect] Processing repo: {repo_name}", flush=True)

        # --- Branch heads ---
        print("[collect] get_branches_data", flush=True)
        branches_data, _ = get_branches_data(repo_path)

        heads = {}
        for branch in branches_data['local_branches']:
            branch_name = branch['name']
            heads[branch_name] = safe_rev_parse(repo_path, branch_name)

        # --- Status ---
        print("[collect] get_git_status_data", flush=True)
        status_data, _ = get_git_status_data(repo_path)

        # --- Remote heads (optional + timeout) ---
        remote_heads = {}
        if include_remote:
            print("[collect] get_remote_heads", flush=True)
            try:
                remote_heads = git_utils.get_remote_heads(repo_path, timeout=3)
            except Exception as e:
                print("[collect] remote head fetch failed:", e, flush=True)

        diff = git_utils.get_diff(repo_path)
   
        # Build structure
        summaries[repo_name] = {
            "heads": heads,
            "status": status_data,
            "remote_heads": remote_heads,
            "diff": diff
        }

        print("collect_repo_summaries status:", status_data, flush=True)
        print(">>>>>>>> >>> >>> >>> summaries:", json.dumps(summaries, indent=4), flush=True)

    return summaries

def collect_and_send_repo_summary(repo_path, include_remote=True):
    repo_name = os.path.basename(repo_path)
    summary = {
        "heads": {},
        "status": {"action_summary": "Error", "action_message": "Failed to collect status"},
        "remote_heads": {},
        "diff": {"diff": "", "diff_info": {"original_size": 0, "status": "complete"}}
    }

    try:
        print(f"[Heartbeat] Processing repo: {repo_name}", flush=True)

        # 1. Local branch heads
        try:
            branches_data, err = get_branches_data(repo_path)
            if err:
                logger.warning(f"[{repo_name}] Failed to get branches: {err}")
            else:
                for branch in branches_data.get('local_branches', []):
                    branch_name = branch['name']
                    head_sha = safe_rev_parse(repo_path, branch_name)
                    if head_sha:
                        summary["heads"][branch_name] = head_sha
        except Exception as e:
            logger.error(f"[{repo_name}] Error getting local heads: {e}")

        # 2. Status
        try:
            status_data, err = get_git_status_data(repo_path)
            if err:
                logger.warning(f"[{repo_name}] Status error: {err}")
            else:
                summary["status"] = status_data or {}
        except Exception as e:
            logger.error(f"[{repo_name}] Fatal status error: {e}")

        # 3. Remote heads (non-blocking)
        if include_remote:
            try:
                remote_heads = git_utils.get_remote_heads(repo_path, timeout=3)
                summary["remote_heads"] = remote_heads or {}
            except Exception as e:
                logger.debug(f"[{repo_name}] Remote heads failed (normal): {e}")

        # 4. Working tree diff
        try:
            diff = git_utils.get_diff(repo_path)
            summary["diff"] = diff or {"diff": "", "diff_info": {"original_size": 0}}
        except Exception as e:
            logger.error(f"[{repo_name}] Diff collection failed: {e}")
            summary["diff"] = {"diff": f"# Error collecting diff: {str(e)}", "diff_info": {"status": "error"}}

        print(f"[{repo_name}] Summary collected successfully", flush=True)
        return repo_name, summary

    except Exception as e:
        logger.error(f"[{repo_name}] Unexpected error in collect_and_send_repo_summary: {e}", exc_info=True)
        summary["status"]["action_message"] = f"Internal error: {str(e)[:100]}"
        return repo_name, summary
    
# Compute delta for a repo/branch
def compute_commit_delta(repo_path, branch, last_head):
    """
    Compute the delta commits for a repo/branch since last_head.
    If last_head is None, returns full history.
    Returns list of commit dicts.
    """
    if not last_head:
        # Full history
        command = [GIT_EXECUTABLE, "-C", repo_path, "log", "--date=iso", "--pretty=format:%H|%P|%an|%ae|%ad|%s", branch]
    else:
        # Delta from last_head..HEAD
        command = [GIT_EXECUTABLE, "-C", repo_path, "log", f"{last_head}..HEAD", "--date=iso", "--pretty=format:%H|%P|%an|%ae|%ad|%s", branch]
    
    # Run the command
    result = subprocess.run(command, cwd=repo_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    if result.returncode != 0:
        # Handle no commits (e.g., initial commit or error)
        stderr = result.stderr.lower()
        if "does not have any commits yet" in stderr or "bad revision" in stderr:
            return [{
                "sha": "00000000",
                "parents": [],
                "author_name": "",
                "author_email": "",
                "date": "",
                "message": "Initial commit"
            }]
        else:
            raise Exception(f"Git command failed: {result.stderr.strip()}")
    
    # Parse the output
    commits = []
    lines = result.stdout.splitlines()
    for line in lines:
        if line.strip():
            parts = line.split("|", 5)
            if len(parts) == 6:
                sha, parents_str, author_name, author_email, date, message = parts
                parents = parents_str.split() if parents_str else []
                commits.append({
                    "sha": sha,
                    "parents": parents,
                    "author_name": author_name,
                    "author_email": author_email,
                    "date": date,
                    "message": message
                })
            else:
                logger.warning(f"Malformed git log line: {line}")
    
    return commits

def find_repo_path_by_name(repo_name):
    for path in REPOSITORIES:
        if os.path.basename(path) == repo_name:
            return path
    return None

# Compute other deltas (e.g., files, branches if HEAD changed)
import subprocess
import os

# Compute other deltas (e.g., files, branches if HEAD changed)
def compute_other_deltas(repo_path, branch, last_head, current_head):
    if last_head == current_head:
        return {}  # No change, skip
    deltas = {}
    # Branches (full if changed)
    branches_data, _ = get_branches_data(repo_path)
    deltas['branches'] = branches_data
    # Remotes (full)
    remotes_output = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "remote", "-v"])
    # Parse remotes as in /api/remotes
    remotes_info = []
    seen = set()  # To avoid duplicates for fetch/push
    for line in remotes_output.splitlines():
        if line.strip():
            parts = line.split()
            if len(parts) >= 3:
                remote_name, remote_url, remote_type = parts[0], parts[1], parts[2].strip('()')
                key = (remote_name, remote_url, remote_type)
                if key not in seen:
                    remotes_info.append({
                        "name": remote_name,
                        "url": remote_url,
                        "type": remote_type
                    })
                    seen.add(key)
    deltas['remotes'] = {"remotes": remotes_info}  # Match structure if needed, or just list
    # deltas['remotes'] = remotes_info # simpler structure MAYBE TBD !!!! TODO INVESTICGATE
    # Files (full ls-tree if changed)
    files, total_size = get_file_list(repo_path)
    deltas['files'] = files
    # README (if exists and changed)
    deltas['readme'] = get_readme_text(repo_path)
    # Status/Diff (always refresh if changed)
    status_data, _ = get_git_status_data(repo_path)
    deltas['status'] = status_data
    # Diff (from /api/diff logic, between last_head and current_head)
    try:
        if last_head and current_head:
            git_command = [GIT_EXECUTABLE, "-C", repo_path, "diff", f"{last_head}..{current_head}"]
        else:
            # Fallback to full HEAD diff if no last_head
            git_command = [GIT_EXECUTABLE, "-C", repo_path, "diff", "HEAD"]
        
        result = subprocess.run(git_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if result.returncode != 0:
            logger.error("Error running git diff: %s", result.stderr)
            deltas['diff'] = {"error": result.stderr.strip()}
        else:
            diff_output = result.stdout
            original_size = len(diff_output)
            # Log warning if diff is very large (e.g., >5MB), but send full
            if original_size > 5 * 1024 * 1024:
                logger.warning("Large diff detected for %s: %d bytes; consider truncation in future", repo_path, original_size)
            deltas['diff'] = {
                "diff": diff_output,
                "diff_info": {
                    "original_size": original_size,
                    "status": "complete"
                }
            }
    except Exception as e:
        logger.error("Exception computing diff: %s", str(e))
        deltas['diff'] = {"error": str(e)}
    
    return deltas

# Helper for token refresh (extracted for reuse)
def refresh_token_and_retry(access_token, func, *args, **kwargs):
    tokens['access_token'] = None
    new_token = get_access_token()
    if new_token:
        logger.info("Token refreshed successfully")
        headers["Authorization"] = f"Bearer {new_token}"
        return func(*args, **kwargs)  # Retry with new headers
    else:
        logger.error("Failed to refresh token; skipping retry")
        return None

# Simple retry wrapper (since no sessions; max 3 attempts with backoff)
def post_with_retry(url, json_data, headers, timeout=10, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=json_data, headers=headers, timeout=timeout)
            if response.status_code // 100 == 5 or response.status_code in [429]:  # Server errors or rate limit
                if attempt < max_retries:
                    time.sleep(attempt)  # Backoff
                    continue
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed (attempt {attempt}): {str(e)}")
            if attempt < max_retries:
                time.sleep(attempt)
            else:
                raise
    raise Exception("Max retries exceeded")

def compute_repo_deltas(repo_name, branch_deltas, summaries):
    """Compute deltas for a single repository."""
    repo_path = find_repo_path_by_name(repo_name)
    if not repo_path:
        logger.warning("Repo %s not found locally; skipping", repo_name)
        return None

    repo_deltas = {}
    for branch, last_head in branch_deltas.items():
        current_head = summaries.get(repo_name, {}).get('heads', {}).get(branch)
        if not current_head:
            logger.warning(f"Current head missing for {repo_name}/{branch}; skipping")
            continue

        commit_delta = compute_commit_delta(repo_path, branch, last_head)
        other_delta = compute_other_deltas(repo_path, branch, last_head, current_head)
        repo_deltas[branch] = {"commits": commit_delta, **other_delta}

        logger.debug(f"Computed delta for {repo_name}/{branch}: {len(commit_delta)} commits")

    return repo_deltas

def send_update(deltas, mode, access_token, headers, base_payload):
    """Send delta update to Stargit with retry + logging."""
    payload = {**base_payload, "mode": "update", "deltas": deltas}
    response = post_with_retry(HEARTBEAT_ENDPOINT, payload, headers, timeout=30)

    if response.status_code == 401:
        response = refresh_token_and_retry(access_token, post_with_retry, HEARTBEAT_ENDPOINT, payload, headers, timeout=30)

    if response.status_code == 200:
        logger.info("%s delta update successful", mode.capitalize())
    else:
        logger.error("%s delta update failed: %s (status: %d)", 
                     mode.capitalize(), response.text, response.status_code)

    return response

# Send heartbeat to stargit.com
def send_heartbeat_to_stargit(batch_mode=False):
    logger.debug(">>>>>>>>>> Sending heartbeat to stargit.com")
    if not STARGIT_API_KEY:
        logger.info("Stargit registration disabled in .env")
        return

    access_token = get_access_token()
    if not access_token:
        logger.error("No valid access token — aborting heartbeat")
        return

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        ip_address = requests.get('https://api.ipify.org', timeout=5).text
    except:
        ip_address = None

    base_payload = {
        "server_uuid": SERVER_UUID,
        "event_type": "heartbeat",
        "status": "online",
        "timestamp": time.time(),
        "ip_address": ip_address
    }

    metrics = collect_server_metrics()

    # === Step 2: Send Metrics ===
    payload = {**base_payload, "mode": "probe", "metrics": metrics}
    response = post_with_retry(HEARTBEAT_ENDPOINT, payload, headers, timeout=15)
    logger.info(">>>>>>>>>> Sending hearbeat metrics")

    if response.status_code == 401:
        response = refresh_token_and_retry(access_token, post_with_retry, HEARTBEAT_ENDPOINT, payload, headers)

    if response.status_code != 200:
        logger.error(f"Heartbeat probe failed: {response.text}")
        return

    successful_repos = []
    failed_repos = []

    for repo_path in REPOSITORIES:
        repo_name = os.path.basename(repo_path)
        logger.info(f">>>>>>>>>> Heartbeat → Processing repository: {repo_name}")

        # === 1. Collect summary for THIS repo only ===
        repo_name_out, summary = collect_and_send_repo_summary(repo_path)
        if repo_name_out != repo_name:
            logger.warning(f"Repo name mismatch: expected {repo_name}, got {repo_name_out}")
            failed_repos.append(repo_name)
            continue

        # === 2. Send PROBE for THIS repo only ===
        probe_payload = {
            **base_payload,
            "mode": "probe",
            "repo_summaries": {repo_name: summary}  # send single repo update
        }

        try:
            response = post_with_retry(HEARTBEAT_ENDPOINT, probe_payload, headers, timeout=15)
            if response.status_code == 401:
                response = refresh_token_and_retry(access_token, post_with_retry,
                                                  HEARTBEAT_ENDPOINT, probe_payload, headers)

            if response.status_code != 200:
                logger.error(f"[{repo_name}] Probe failed: {response.text}")
                failed_repos.append(repo_name)
                continue

            data = response.json()
            needed_deltas = data.get("needed_deltas", {})

            # === 3. If server wants delta → compute and send immediately ===
            if needed_deltas and repo_name in needed_deltas:
                branch_deltas = needed_deltas[repo_name]
                try:
                    delta = compute_repo_deltas(repo_name, branch_deltas, {repo_name: summary})
                    if delta:
                        delta_payload = {
                            **base_payload,
                            "mode": "update",
                            "deltas": {repo_name: delta}
                        }
                        resp = post_with_retry(HEARTBEAT_ENDPOINT, delta_payload, headers, timeout=30)
                        if resp.status_code == 200:
                            logger.info(f"[{repo_name}] Full sync completed")
                        else:
                            logger.error(f"[{repo_name}] Delta send failed: {resp.text}")
                except Exception as e:
                    logger.error(f"[{repo_name}] Delta computation failed: {e}", exc_info=True)

            successful_repos.append(repo_name)
            logger.info(f"[{repo_name}] Heartbeat cycle completed successfully")

        except Exception as e:
            logger.error(f"[{repo_name}] Unexpected error in heartbeat cycle: {e}", exc_info=True)
            failed_repos.append(repo_name)

    # === Final Summary ===
    total = len(REPOSITORIES)
    logger.info(f"Heartbeat completed: {len(successful_repos)}/{total} repos synced")
    if failed_repos:
        logger.warning(f"Failed repos: {', '.join(failed_repos)}")



def register_with_stargit(event_type='online'):
    """Send registration or heartbeat to stargit.com using token."""
    logger.debug(f"Registering with stargit.com, event_type={event_type}")
    if not STARGIT_API_KEY:
        logger.info("Stargit registration disabled in .env")
        return
    
    access_token = get_access_token()
    if not access_token or not SERVER_UUID:
        logger.error("No valid access token or server UUID; registration aborted (key: %s...)", STARGIT_API_KEY[:8] if STARGIT_API_KEY else "None")
        return
    else:
        logger.info("Valid access token recieved %s: ", access_token)

    try:
        metrics = collect_server_metrics() if event_type == 'heartbeat' else {}
        payload = {
            "server_uuid": SERVER_UUID,
            "event_type": event_type,
            "status": "online",
            "metrics": metrics,
            "timestamp": time.time(),
            "ip_address": requests.get('https://api.ipify.org').text if event_type == 'online' else None
        }
        # Include detailed repo info only in heartbeats if PUSH_MODE is enabled
        if event_type == 'heartbeat' and PUSH_MODE:
            payload["repositories"] = collect_repo_details()
            #logger.info("repositories:\n%s", json.dumps(payload["repositories"], indent=4, default=str))
            logger.debug("Including detailed repository information in heartbeat payload due to PUSH_MODE=true")
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        endpoint = REGISTER_ENDPOINT if event_type == 'online' else HEARTBEAT_ENDPOINT

        logger.info(">>>> access_token %s", access_token)
        logger.info("*** Calling %s ", endpoint)
        logger.info("*** *** payload %s ", payload)
        logger.info("*** *** **** headers %s ", headers)
        response = requests.post(endpoint, json=payload, headers=headers)
        if response.status_code == 200:
            logger.info("Successfully sent %s to %s: %s", event_type, endpoint, response.json())
        elif response.status_code == 401:
            logger.warning("Token invalid or expired, attempting to refresh")
            exit(0)
            tokens['access_token'] = None
            access_token = get_access_token()
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
                response = requests.post(endpoint, json=payload, headers=headers)
                if response.status_code == 200:
                    logger.info("Successfully sent %s after token refresh: %s", event_type, response.json())
                else:
                    logger.error("Failed to send %s after refresh to %s: %s (status: %d)", event_type, endpoint, response.text, response.status_code)
            else:
                logger.error("Failed to refresh token; %s aborted", event_type)
        else:
            logger.error("Failed to send %s to %s: %s (status: %d)", event_type, endpoint, response.text, response.status_code)
    except Exception as e:
        logger.error("Error during %s to %s: %s", event_type, endpoint, str(e))

def registration_thread():
    """Background thread for initial registration and periodic heartbeats."""
    print("------------------------------------", flush=True)
    print(">>>>>>>>>>>>>>>> registration_thread", flush=True)
    if STARGIT_API_KEY:
        print(">>>>>>>>>>>>>>>> registration_thread using stargit api key", STARGIT_API_KEY, flush=True)
        register_with_stargit(event_type='online')  # Initial registration
        while True:
            send_heartbeat_to_stargit()  # Periodic heartbeat
            time.sleep(300)  # 5 minutes
    else:
        print("<<<<<<<<<<<< Invalid stargit api key", STARGIT_API_KEY, flush=True)

# Binary-safe file fetch (git show returns bytes)
def get_file_content(repo_path, file_path, ref='HEAD'):
    command = [GIT_EXECUTABLE, "-C", repo_path, "show", f"{ref}:{file_path}"]
    try:
        result = subprocess.run(command, cwd=repo_path, capture_output=True, text=False, timeout=30)
        if result.returncode != 0:
            return None, result.stderr.decode('utf-8', errors='ignore')
        
        content = result.stdout  # bytes
        mime, _ = mimetypes.guess_type(file_path)
        
        # Attempt decode to check if text
        try:
            content_str = content.decode('utf-8')
            # If decodes, it's text: no base64, default mime if None
            mime = mime or 'text/plain'
            return {"content": content_str, "is_base64": False, "mime_type": mime}, None
        except UnicodeDecodeError:
            # Binary or invalid text: base64, default mime if None
            mime = mime or 'application/octet-stream'
            encoded = base64.b64encode(content).decode('ascii')
            return {"content": encoded, "is_base64": True, "mime_type": mime}, None
    except Exception as e:
        return None, str(e)

# Find repo path by name (assuming REPOSITORIES is list of paths)
def find_repo_path(repo_name):
    for path in REPOSITORIES:
        if os.path.basename(path) == repo_name:
            return path
    return None

# Get file commit history
def get_file_history(repo_path, file_path, ref='HEAD'):
    """Get commit history for a file: list of commits that modified it, with basic info and changes."""
    command = [GIT_EXECUTABLE, "-C", repo_path, "log", "--pretty=format:%H|%an|%ae|%ad|%s", "--date=iso", "--numstat", ref, "--", file_path]
    output = run_git_command(repo_path, command)
    if output.startswith("Error:"):
        logger.warning("Failed to get file history for %s in %s", file_path, repo_path)
        return None
    history = []
    current_commit = None
    for line in output.splitlines():
        if line.strip():
            if '|' in line:  # Commit line
                if current_commit:
                    history.append(current_commit)
                parts = line.split('|', 4)
                if len(parts) == 5:
                    sha, author_name, author_email, date, message = parts
                    current_commit = {
                        "sha": sha,
                        "author_name": author_name,
                        "author_email": author_email,
                        "date": date,
                        "message": message,
                        "changes": {"additions": 0, "deletions": 0}  # Aggregate numstat
                    }
            elif current_commit and '\t' in line:  # Numstat line: additions\tdeletions\tpath
                parts = line.split('\t')
                if len(parts) == 3:
                    add, del_, path = parts
                    if add.isdigit() and del_.isdigit():
                        current_commit["changes"]["additions"] += int(add)
                        current_commit["changes"]["deletions"] += int(del_)
    if current_commit:
        history.append(current_commit)
    return history

def get_commit_diff(repo_path, commit_sha):
    try:
        # Get name-status
        command = [GIT_EXECUTABLE, "-C", repo_path, "diff", "--name-status", f"{commit_sha}^..{commit_sha}"]
        output = run_git_command(repo_path, command)
        if output.startswith("Error:"):
            logger.warning("Failed to get name-status for commit %s in %s", commit_sha, repo_path)
            return None
        changed_files = []
        for line in output.splitlines():
            if line.strip():
                parts = line.split('\t')
                status = parts[0]
                if status.startswith('R'):
                    old_file, new_file = parts[1], parts[2]
                    #changed_files.append({'old_filename': old_file, 'filename': new_file, 'status': 'renamed', 'additions': 0, 'deletions': 0, 'binary': False, 'old_content': None, 'new_content': None})
                    changed_files.append({'old_filename': old_file, 'filename': new_file, 'status': 'renamed', 'additions': 0, 'deletions': 0, 'binary': False, 'diff': None})
                else:
                    filename = parts[1]
                    #changed_files.append({'filename': filename, 'status': {'A': 'added', 'M': 'modified', 'D': 'deleted'}.get(status, 'unknown'), 'additions': 0, 'deletions': 0, 'binary': False, 'old_content': None, 'new_content': None})
                    changed_files.append({'filename': filename, 'status': {'A': 'added', 'M': 'modified', 'D': 'deleted'}.get(status, 'unknown'), 'additions': 0, 'deletions': 0, 'binary': False, 'diff': None})

        # Get numstat
        command = [GIT_EXECUTABLE, "-C", repo_path, "diff", "--numstat", f"{commit_sha}^..{commit_sha}"]
        output = run_git_command(repo_path, command)
        num_map = {}
        for line in output.splitlines():
            if line.strip():
                add, del_, file = line.split('\t')
                num_map[file] = {'additions': int(add) if add != '-' else 0, 'deletions': int(del_) if del_ != '-' else 0}

        # Get contents
        if False:
            for f in changed_files:
                filename = f['filename']
                f['additions'] = num_map.get(filename, {}).get('additions', 0)
                f['deletions'] = num_map.get(filename, {}).get('deletions', 0)
                if f['status'] != 'deleted':
                    command = [GIT_EXECUTABLE, "-C", repo_path, "show", f"{commit_sha}:{filename}"]
                    output = run_git_command(repo_path, command)
                    f['new_content'] = output if not output.startswith("Error:") else None
                if f['status'] != 'added':
                    old_filename = f.get('old_filename', filename)
                    command = [GIT_EXECUTABLE, "-C", repo_path, "show", f"{commit_sha}^:{old_filename}"]
                    output = run_git_command(repo_path, command)
                    f['old_content'] = output if not output.startswith("Error:") else None
                if f['old_content'] is None and f['new_content'] is None:
                    f['binary'] = True

        # Get raw diffs
        for f in changed_files:
            filename = f['filename']
            f['additions'] = num_map.get(filename, {}).get('additions', 0)
            f['deletions'] = num_map.get(filename, {}).get('deletions', 0)
            if f['status'] in ['modified', 'added', 'deleted', 'renamed']:
                old_filename = f.get('old_filename', filename)
                command = [GIT_EXECUTABLE, "-C", repo_path, "diff", f"{commit_sha}^", f"{commit_sha}", "--", old_filename]
                output = run_git_command(repo_path, command)
                f['diff'] = output if not output.startswith("Error:") else None
                if f['diff'] and "Binary files" in f['diff']:
                    f['binary'] = True
                    f['diff'] = "Binary file differs"

        return changed_files
    except Exception as e:
        logger.error(f"Error getting commit diff for {commit_sha}: {str(e)}")
        return None

def get_new_commits_and_diff(repo_path, old_head_sha=None):
    """
    Return new commits since old_head_sha (newest-first) and current working tree diff.

    :param repo_path: Path to the Git repository
    :param old_head_sha: SHA of the previous HEAD (None for first commit)
    :return: (current_head_sha, list_of_new_commits, working_tree_diff)
    """
    try:
        # Get current HEAD
        current_head = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "rev-parse", "HEAD"]).strip()
        if not current_head or current_head.startswith("Error:"):
            return None, [], git_utils.get_diff(repo_path)

        new_commits = []

        # Only get new commits if old_head_sha is set and differs from current HEAD
        if old_head_sha and old_head_sha != current_head:
            log_cmd = [
                GIT_EXECUTABLE, "-C", repo_path, "log",
                f"{old_head_sha}..{current_head}",
                "--pretty=format:%H|%P|%an|%ae|%ad|%s", "--date=iso"
            ]
            log_output = run_git_command(repo_path, log_cmd)

            for line in log_output.splitlines():
                if not line.strip():
                    continue
                parts = line.split("|", 5)
                if len(parts) < 6:
                    continue
                sha, parents_str, author_name, author_email, date, message = parts
                parents = parents_str.split() if parents_str else []
                new_commits.append({
                    "sha": sha,
                    "parents": parents,
                    "author_name": author_name,
                    "author_email": author_email,
                    "date": date,
                    "message": message
                })

        # Get working tree diff
        diff_data = git_utils.get_diff(repo_path)

        return current_head, new_commits, diff_data

    except Exception as e:
        logger.error(f"Failed to get new commits/diff: {e}")
        return None, [], None
    
def get_head(repo_path):
    """
    Safely returns the HEAD state.
    Returns one of:
        None                        → no commits yet
        commit_hash                 → normal HEAD
        ("DETACHED", commit_hash)   → detached HEAD state
    """
    try:
        result = subprocess.run(
            [GIT_EXECUTABLE, "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True
        )

        if result.returncode != 0:
            msg = (result.stderr + result.stdout).lower()
            # Cases like: empty repo / no commits
            if "unknown revision" in msg or "needed a single revision" in msg or "fatal:" in msg:
                return None
            return None

        head_hash = result.stdout.strip()

        # Detect detached HEAD
        symbolic = subprocess.run(
            [GIT_EXECUTABLE, "-C", repo_path, "symbolic-ref", "-q", "HEAD"],
            capture_output=True, text=True
        )

        if symbolic.returncode != 0:  # Not pointing to refs/heads/* → detached
            return ("DETACHED", head_hash)

        return head_hash

    except Exception as e:
        logger.warning(f"[StarBridge] Failed to read HEAD: {e}")
        return None
    
# Process tasks from poll response - handles 'get_file' and 'get_file_history' actions
def process_tasks(tasks):
    logger.debug(f"Processing {len(tasks)} tasks")
    results = []

    # Actions that modify the working tree / index and should return fresh status
    STATUS_CHANGING_ACTIONS = {
        "resolve_conflict",
        "continue_merge",
        "stage_file",
        "unstage_file",
        "discard_file",
        "commit",
        "reset",            # future
        "checkout_file",    # future
        "push",
        "pull",
        "create_file"
    }

    HEADS_CHANGING_ACTIONS = {
        "continue_merge",
        "commit",
        "reset",            # future
        "checkout_file",    # future
        "push",
        "pull"
    }

    for task in tasks:
        logger.debug(f"Task details: {task}")
        action = task.get('action')
        params = task.get('params', {})
        repo_name = params.get('repo_name')

        if action == "create_repo":
            repo_path = None
        else:
            repo_path = find_repo_path_by_name(repo_name)

            if not repo_path:
                logger.warning(f"Repo {repo_name} not found")
                results.append({"task_id": task['id'], "result": None, "error": f"Repo {repo_name} not found"})
                continue
        
        task_result = {"task_id": task['id'], 'repo_name': repo_name}
        needs_status_refresh = action in STATUS_CHANGING_ACTIONS
        needs_heads_refresh = action in HEADS_CHANGING_ACTIONS

        # Capture old/previous remote heads
        if action in HEADS_CHANGING_ACTIONS:
            task["previous_remote_heads"] = git_utils.get_remote_heads(repo_path) or {}

        if action == 'get_file':
            file_path = params.get('file_path')
            commit_sha = params.get('commit_sha', 'HEAD')
            logger.debug(f"get_file params: repo={repo_name}, path={file_path}, sha={commit_sha}")
            content_data, error = get_file_content(repo_path, file_path, commit_sha)
            if error:
                logger.error(f"get_file error: {error}")
                #results.append({"task_id": task['id'], "result": None, "error": error})
                task_result.update({"result": None, "error": error})
            else:
                logger.debug(f"get_file success: {content_data}")
                #results.append({"task_id": task['id'], "result": content_data, "error": None})
                task_result.update({"result": content_data, "error": None})
        
        elif action == "create_file":
            
            file_path = params.get("file_path")      # e.g. ".stargit/ci.yml"
            content = params.get("content", "")
            overwrite = params.get("overwrite", False)
            git_add = params.get("git_add", False)
            logger.debug(f"create_file params: repo={repo_name}, path={file_path}, overwrite={overwrite}")
            
            full_path = os.path.join(repo_path, file_path)

            # Security: prevent path traversal
            full_path = os.path.abspath(full_path)
            repo_path_abs = os.path.abspath(repo_path)

            if not full_path.startswith(repo_path_abs + os.sep):
                task_result.update({"error": "Invalid file path (outside repo)"})
                continue

            try:
                # Create parent directories
                os.makedirs(os.path.dirname(full_path), exist_ok=True)

                # Write file
                mode = 'w' if overwrite else 'x'
                with open(full_path, mode, encoding="utf-8") as f:
                    f.write(content)

                if git_add:
                    add_result = subprocess.run(
                        [GIT_EXECUTABLE, "-C", repo_path, "add", file_path],
                        capture_output=True,
                        text=True
                    )

                    if add_result.returncode == 0:
                        logger.info(f"Auto-added {file_path} to index")
                    else:
                        logger.warning(f"git add failed for {file_path}: {add_result.stderr}")

                task_result.update({
                    "result": {
                        "status": "file_created",
                        "file_path": file_path,
                        "git_added": git_add and add_result.returncode == 0,
                        "size_bytes": len(content)
                    }
                })
                
                logger.info(f"Created file {file_path} in {repo_name}")

            except FileExistsError:
                task_result.update({"error": f"File {file_path} already exists (use overwrite=true)"})
            except Exception as e:
                task_result.update({"error": str(e)})
                logger.error(f"Failed to create file {file_path}: {e}")

        elif action == 'get_file_history':
            file_path = params.get('file_path')
            commit_sha = params.get('commit_sha', 'HEAD')
            logger.debug(f"get_file_history params: repo={repo_name}, path={file_path}, sha={commit_sha}")
            history = get_file_history(repo_path, file_path, commit_sha)
            if history is None:
                #results.append({"task_id": task['id'], "result": None, "error": "Failed to fetch history"})
                task_result.update({"result": None, "error": "Failed to fetch history"})
            else:
                #results.append({"task_id": task['id'], "result": {"history": history}, "error": None})
                task_result.update({"result": {"history": history}, "error": None})
        
        elif action == 'get_commit_diff':
            commit_sha = params.get('commit_sha')
            logger.debug(f"get_commit_diff params: repo={repo_name}, sha={commit_sha}")
            files = get_commit_diff(repo_path, commit_sha)
            if files is None:
                #results.append({"task_id": task['id'], "result": None, "error": "Failed to fetch commit diff"})
                task_result.update({"result": None, "error": "Failed to fetch commit diff"})
            else:
                #results.append({"task_id": task['id'], "result": {"files": files}, "error": None})
                task_result.update({"result": {"files": files}, "error": None})

        elif action == 'resolve_conflict':
            params = task.get('params', {})
            repo_name = params.get('repo_name')
            file_path = params.get('file_path')
            resolution = params.get('resolution')  # ours, theirs, local, content

            repo_path = find_repo_path(repo_name)
            if not repo_path:
                results.append({"task_id": task['id'], "error": "Repo not found"})
                continue

            try:
                full_path = os.path.join(repo_path, file_path)

                if resolution == "ours":
                    subprocess.run([GIT_EXECUTABLE, "-C", repo_path, "checkout", "--ours", file_path], check=True, capture_output=True)
                    logger.info(f"Resolved {file_path}: Kept OURS")

                elif resolution == "theirs":
                    subprocess.run([GIT_EXECUTABLE, "-C", repo_path, "checkout", "--theirs", file_path], check=True, capture_output=True)
                    logger.info(f"Resolved {file_path}: Kept THEIRS")

                elif resolution == "local":
                    # Just stage current working tree version (no checkout)
                    logger.info(f"Resolved {file_path}: Kept CURRENT working tree version")
                    pass  # Nothing to do — file is already as user wants

                elif resolution == "content":
                    content = params.get('content', '')
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    logger.info(f"Resolved {file_path}: Replaced with provided content")

                else:
                    results.append({"task_id": task['id'], "error": f"Invalid resolution: {resolution}"})
                    continue

                # Always stage the final version
                result = subprocess.run([GIT_EXECUTABLE, "-C", repo_path, "add", file_path], capture_output=True, text=True)
                if result.returncode != 0:
                    results.append({"task_id": task['id'], "error": f"git add failed: {result.stderr}"})
                    continue

                task_result.update({
                    "result": {
                        "status": "resolved",
                        "resolution": resolution,
                        "file": file_path
                    }
                })

            except Exception as e:
                logger.error(f"Conflict resolution failed for {file_path}: {str(e)}")
                #results.append({"task_id": task['id'], "error": str(e)})
                results.append({"task_id": task['id'], "error": str(e)})
                continue

        elif action == 'continue_merge':
            logger.debug(">>>>>>>>> continue_merge >>>>>>>>>")

            try:
                params = task.get('params') or {}
                repo_name = params.get('repo_name')
                commit_message = params.get('commit_message')
                name = params.get('name')
                email = params.get('email')

                # Validate parameters
                if not repo_name:
                    results.append({"task_id": task["id"], "error": "Missing repo_name"})
                    continue
                if not commit_message:
                    results.append({"task_id": task["id"], "error": "Missing commit_message"})
                    continue

                repo_path = find_repo_path(repo_name)
                if not repo_path:
                    results.append({"task_id": task["id"], "error": f"Repository '{repo_name}' not found"})
                    continue

                # Prepare environment with committer identity
                env = os.environ.copy()
                env["GIT_EDITOR"] = "true"                    # ← No editor
                env["GIT_TERMINAL_PROMPT"] = "0"              # ← No prompts
                env["GIT_MERGE_AUTOEDIT"] = "no"              # ← No auto-edit
                if name and email:
                    env["GIT_AUTHOR_NAME"] = name
                    env["GIT_AUTHOR_EMAIL"] = email
                    env["GIT_COMMITTER_NAME"] = name
                    env["GIT_COMMITTER_EMAIL"] = email
                    
                # Detect merge or rebase in progress
                git_dir = os.path.join(repo_path, ".git")

                is_rebase = (
                    os.path.exists(os.path.join(git_dir, "rebase-merge")) or
                    os.path.exists(os.path.join(git_dir, "rebase-apply"))
                )
                is_merge = os.path.exists(os.path.join(git_dir, "MERGE_HEAD"))

                logger.debug(f"Repo path: {repo_path}")
                logger.debug(f"is_rebase={is_rebase}, is_merge={is_merge}")

                if not is_merge and not is_rebase:
                    results.append({
                        "task_id": task["id"],
                        "error": "No merge or rebase is currently in progress"
                    })
                    continue

                # First: try merge --continue if merge in progress
                merge_completed = False
                if is_merge:
                    logger.debug("Attempting: git merge --continue")

                    merge_cmd = [
                        GIT_EXECUTABLE, "-C", repo_path,
                        "merge", "--continue"
                    ]

                    merge_result = subprocess.run(
                        merge_cmd, capture_output=True, text=True, env=env
                    )

                    logger.debug({
                        "merge_stdout": merge_result.stdout,
                        "merge_stderr": merge_result.stderr,
                        "return_code": merge_result.returncode
                    })

                    if merge_result.returncode == 0:
                        logger.info({"task_id": task["id"], "result": {"status": "merge_completed"}})
                        task_result.update({"result": {"status": "merge_completed"}})
                        merge_completed = True

                    # If merge failed and no rebase is present → fatal
                    elif not is_rebase:
                        result.append({
                            "task_id": task["id"],
                            "error": merge_result.stderr or merge_result.stdout or "Unknown merge error"
                        })
                        continue 

                if merge_completed == False:
                    # If here → merge didn't apply OR we are in rebase → try rebase --continue
                    logger.debug("Attempting: git rebase --continue")

                    # For rebase, Git uses an internal commit message — but we can write ours into .git/rebase-merge/message
                    rebase_msg_path = os.path.join(git_dir, "rebase-merge", "message")
                    if is_rebase:
                        try:
                            os.makedirs(os.path.dirname(rebase_msg_path), exist_ok=True)
                            with open(rebase_msg_path, "w", encoding="utf-8") as f:
                                f.write(commit_message)
                            logger.debug("Custom commit message written to rebase-merge/message")
                        except Exception as msg_err:
                            logger.error(f"Failed to write rebase commit message: {msg_err}")

                    rebase_cmd = [
                        GIT_EXECUTABLE, "-C", repo_path,
                        "rebase", "--continue"
                    ]

                    rebase_result = subprocess.run(
                        rebase_cmd, capture_output=True, text=True, env=env
                    )

                    logger.debug({
                        "rebase_stdout": rebase_result.stdout,
                        "rebase_stderr": rebase_result.stderr,
                        "return_code": rebase_result.returncode
                    })

                    if rebase_result.returncode == 0:
                        task_result.update({"result": {"status": "merge_completed"}})
                    else:
                        results.append({
                            "task_id": task["id"],
                            "error": rebase_result.stderr or rebase_result.stdout or "Unknown rebase error"
                        })
                        continue

            except Exception as e:
                logger.error(f"Merge Continue failed for {repo_name}: {str(e)}")
                results.append({"task_id": task["id"], "error": str(e)})
                continue

        elif action == "stage_file":
            params = task.get('params', {})
            repo_name = params.get('repo_name')
            file_path = params.get('file_path')

            repo_path = find_repo_path(repo_name)
            if not repo_path:
                results.append({"task_id": task['id'], "error": "Repo not found"})
                continue

            try:
                subprocess.run([GIT_EXECUTABLE, "-C", repo_path, "add", file_path], check=True, capture_output=True)
                task_result.update({
                    "result": {
                        "status": "staged", 
                        "file": file_path
                    }
                })
                logger.info(f"Staged: {file_path}")
            except Exception as e:
                results.append({"task_id": task['id'], "error": str(e)})
                continue

        elif action == "unstage_file":
            params = task.get('params', {})
            repo_name = params.get('repo_name')
            file_path = params.get('file_path')

            repo_path = find_repo_path(repo_name)
            if not repo_path:
                results.append({"task_id": task['id'], "error": "Repo not found"})
                continue

            try:
                subprocess.run([GIT_EXECUTABLE, "-C", repo_path, "restore", "--staged", file_path], check=True, capture_output=True)
                task_result.update({
                    "result": {"status": "unstaged", "file": file_path}
                })
                logger.info(f"Unstaged: {file_path}")
            except Exception as e:
                results.append({"task_id": task['id'], "error": str(e)})
                continue

        elif action == "discard_file":
            params = task.get('params', {})
            repo_name = params.get('repo_name')
            file_path = params.get('file_path')

            repo_path = find_repo_path(repo_name)
            if not repo_path:
                results.append({"task_id": task['id'], "error": "Repo not found"})
                continue

            try:
                subprocess.run([GIT_EXECUTABLE, "-C", repo_path, "restore", file_path], check=True, capture_output=True)
                task_result.update({
                    "result": {"status": "discarded", "file": file_path}
                })
                logger.info(f"Discarded changes: {file_path}")
            except Exception as e:
                results.append({"task_id": task['id'], "error": str(e)})
                continue
        
        elif action == "push":
            
            remote = params.get('remote', 'origin')
            branch = params.get('branch', 'HEAD')
            force = params.get('force', False)

            try:
                cmd = [GIT_EXECUTABLE, "-C", repo_path, "push"]
                if force:
                    cmd.append("--force-with-lease")
                cmd.extend([remote, branch])

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                
                if result.returncode == 0:
                    task_result.update({
                        "result": {
                            "status": "pushed",
                            "output": result.stdout
                        }
                    })
                else:
                    task_result.update({
                        "error": result.stderr or "Push failed"
                    })
            except Exception as e:
                task_result.update({"error": str(e)})

        elif action == "commit":
            message = params.get('commit_message', '').strip()
            name = params.get('name')
            email = params.get('email')

            if not message:
                results.append({"task_id": task['id'], "error": "Commit message required"})
                continue

            try:

                
                # CAPTURE OLD HEAD BEFORE COMMIT
                old_head_sha = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "rev-parse", "HEAD"]).strip()
                if old_head_sha.startswith("Error:") or not old_head_sha:
                    old_head_sha = None  # First commit case



                env = os.environ.copy()

                if name and email:
                    env["GIT_AUTHOR_NAME"] = name
                    env["GIT_AUTHOR_EMAIL"] = email
                    env["GIT_COMMITTER_NAME"] = name
                    env["GIT_COMMITTER_EMAIL"] = email

                    author_info = f"{name} <{email}>"
                    print("commiting with provided author info:", author_info, flush=True)
                    cmd = [GIT_EXECUTABLE, "-C", repo_path, "commit", "--author", author_info, "-a", "-m", message]
                else:
                    cmd = [GIT_EXECUTABLE, "-C", repo_path, "commit", "-a", "-m", message]
                
                result = subprocess.run(cmd, capture_output=True, text=True, env=env)

                if result.returncode == 0:

                    # Now get fresh status + new commits
                    new_head_sha, new_commits, diff_data = get_new_commits_and_diff(repo_path, old_head_sha or "")

                    task_result.update({
                        "result": {
                            "status": "committed",
                            "message": message,
                            "old_head": old_head_sha,
                            "new_head": new_head_sha,
                            "new_commits": new_commits or [],
                            "diff": diff_data
                        }
                    })
                    logger.info(f"Commit successful: {repo_path}")

                else:
                    full_error = f"Commit failed (exit {result.returncode})\n\n"
                    
                    if result.stderr:
                        full_error += "Git error output:\n"
                        full_error += result.stderr.strip()
                    else:
                        full_error += "No error output from Git"
                    
                    if result.stdout:
                        full_error += "\n\nGit output:\n"
                        full_error += result.stdout.strip()

                    logger.error(
                        f"Commit FAILED for {repo_name}\n"
                        f"Command: {' '.join(cmd)}\n"
                        f"Exit code: {result.returncode}\n"
                        f"STDERR:\n{result.stderr}\n"
                        f"STDOUT:\n{result.stdout}"
                    )

                    results.append({
                        "task_id": task['id'],
                        "error": full_error,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr
                    })
                    continue

            except Exception as e:
                logger.exception("Exception during commit")
                results.append({"task_id": task['id'], "error": str(e)})
                continue

        elif action == "pull":
            logger.info(f"[StarBridge] Pull requested for {repo_path}")

            remote = params.get('remote', 'origin')
            branch = params.get('branch')
            pull_mode = params.get('pull_mode', 'ff-only')
            name = params.get('name')
            email = params.get('email')

            if not branch:
                results.append({"task_id": task['id'], "error": "Branch not specified"})
                continue

            # --- 1. Capture old HEAD (supports empty repo + detached HEAD) ---
            old_head = get_head(repo_path)

            if old_head is None:
                logger.info("[StarBridge] Old HEAD: none (empty repo)")
            elif isinstance(old_head, tuple):
                logger.info(f"[StarBridge] Old HEAD: DETACHED at {old_head[1]}")
            else:
                logger.info(f"[StarBridge] Old HEAD: {old_head}")

            # --- 2. Prepare pull command ---
            pull_cmd = [GIT_EXECUTABLE, "-C", repo_path, "pull"]

            if pull_mode == "rebase":
                pull_cmd.append("--rebase")
            elif pull_mode == "ff-only":
                pull_cmd.append("--ff-only")
            elif pull_mode == "merge":
                pull_cmd.append("--no-rebase")
            elif pull_mode is not None:
                results.append({"task_id": task['id'], "error": f"Invalid pull_mode '{pull_mode}'"})
                continue
                
            pull_cmd.append(remote)
            pull_cmd.append(branch)
            
            # Prepare environment with committer identity
            env = os.environ.copy()
            if name and email:
                env["GIT_AUTHOR_NAME"] = name
                env["GIT_AUTHOR_EMAIL"] = email
                env["GIT_COMMITTER_NAME"] = name
                env["GIT_COMMITTER_EMAIL"] = email
                
            # --- 3. Execute pull ---
            result = subprocess.run(pull_cmd, capture_output=True, text=True, env=env)

            if result.returncode != 0:
                # Combine stdout + stderr for more info
                error_msg = "\n".join(filter(None, [
                    result.stderr.strip(),
                    result.stdout.strip()
                ])) or "Unknown pull error"

                logger.error(f"[StarBridge] Pull failed for {repo_path} ({remote}/{branch}): {error_msg}")
                
                results.append({
                    "task_id": task['id'],
                    "error": "pull_failed: " + error_msg,
                    "remote": remote,
                    "branch": branch
                })
                continue

            # --- 4. Determine new HEAD ---
            new_head = get_head(repo_path)

            if new_head is None:
                logger.info("[StarBridge] New HEAD: none (still empty?)")
            elif isinstance(new_head, tuple):
                logger.info(f"[StarBridge] New HEAD: DETACHED at {new_head[1]}")
            else:
                logger.info(f"[StarBridge] New HEAD: {new_head}")

            # --- 5. Detect newly fetched commits + live diff ---
            new_commits, diff_data = [], None

            # Only compute diff when a meaningful change occurred
            def head_value(h):
                return h[1] if isinstance(h, tuple) else h

            if head_value(old_head) != head_value(new_head) and head_value(new_head) is not None:
                try:
                    _, new_commits, diff_data = get_new_commits_and_diff(
                        repo_path,
                        head_value(old_head)
                    )
                except Exception as e:
                    logger.exception(f"[StarBridge] Failed computing commits/diff: {e}")

            # --- 6. Build final response ---
            task_result.update({
                "result": {
                    "status": "pulled",
                    "remote": remote,
                    "branch": branch,
                    "old_head": old_head,
                    "old_detached": isinstance(old_head, tuple),
                    "new_head": new_head,
                    "new_detached": isinstance(new_head, tuple),
                    "new_commits": new_commits or [],
                    "diff": diff_data,
                    "pull_output": result.stdout.strip()
                }
            })

            logger.info(
                f"[StarBridge] Pull completed for {repo_path} ({remote}/{branch}): "
                f"{len(new_commits or [])} new commits."
            )

        elif action == "reset_hard":
            
            target = params.get('target', 'HEAD')  # e.g. "HEAD~3" or commit SHA

            try:
                # --- 1. Save current state as backup diff (undo safety) ---
                backup_msg = f"StarGit backup before reset hard ({datetime.utcnow().isoformat()})"
                
                now_utc = datetime.now(timezone.utc)
                timestamp = now_utc.strftime("%Y-%m-%d_%H-%M-%S")

                # --- Create backup folder ---
                backup_dir = os.path.join(repo_path, ".stargit", "backups", timestamp)
                os.makedirs(backup_dir, exist_ok=True)

                # ---  Save backup message to file ---
                backup_msg_file = os.path.join(backup_dir, f"{timestamp}_backup_commit_message.txt")
                with open(backup_msg_file, "w", encoding="utf-8") as f:
                    f.write(backup_msg)
                # --- 4. Save current working tree diff ---
                try:
                    pre_diff = git_utils.get_diff(repo_path)
                    if pre_diff:
                        diff_file = os.path.join(backup_dir, f"{timestamp}_pre_reset_diff.diff")
                        with open(diff_file, "w", encoding="utf-8") as f:
                            f.write(json.dumps(pre_diff, indent=2))
                           
                except Exception as e:
                    logger.warning(f"Failed to get pre-reset diff: {e}")
                    pre_diff = None

                # 2. Get diff before reset (for UI)
                try:
                    pre_diff = git_utils.get_diff(repo_path)
                except Exception as e:
                    logger.warning(f"Failed to get pre-reset diff: {e}")
                    pre_diff = None

                # 3. Hard reset
                cmd = [GIT_EXECUTABLE, "-C", repo_path, "reset", "--hard", target]
                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    error_text = (result.stderr or result.stdout or "Reset failed").strip()
                    logger.error(f"Hard reset failed: {error_text}")
                    results.append({"task_id": task['id'], "error": f"reset_failed: {error_text}"})
                    continue

                # 4. Fresh status + diff
                try:
                    fresh_status, _ = get_git_status_data(repo_path)
                except Exception as e:
                    logger.warning(f"Failed to get repo status after reset: {e}")
                    fresh_status = {}

                try:
                    post_diff = git_utils.get_diff(repo_path)
                except Exception as e:
                    logger.warning(f"Failed to get post-reset diff: {e}")
                    post_diff = None

                # --- 5. Append result ---
                task_result.update({
                    "result": {
                        "status": "reset_hard_complete",
                        "target": target,
                        "backup_message": f"Backup saved at {timestamp}",
                        "backup_path": f".stargit/backups/{timestamp}",
                        "pre_reset_diff": pre_diff,
                        "post_reset_diff": post_diff,
                        "repo_status": fresh_status
                    }
                })

                logger.info(f"[StarBridge] Hard reset to {target} completed with backup commit")

            except Exception as e:
                logger.exception(f"Exception during reset_hard: {str(e)}")
                results.append({"task_id": task['id'], "error": str(e)})
                
        elif action == "get_status":

            try:
                logger.info(f"[StarBridge] Getting status for repo: {repo_name}")
                output = run_git_command(repo_path, [GIT_EXECUTABLE, "-C", repo_path, "status"])
                output = output.strip()  # Remove leading/trailing newlines

                task_result.update({
                    "result": {
                        "status_output": output
                    }
                })
            except Exception as e:
                logger.exception(f"[StarBridge] Exception while getting status for {repo_name}")
                results.append({"task_id": task['id'], "error": str(e)})

        elif action == "sync_remote_heads":
            
            
            try:
                # Optionally refresh ahead/behind
                remote_heads = git_utils.get_remote_heads(repo_path)
                ahead, behind = git_utils.get_ahead_behind(repo_path)
                if "result" not in task_result:
                    task_result["result"] = {}
                task_result.update({
                    "result": {
                        "status": "remote_heads_synced",
                        "remote_heads": remote_heads,
                        "ahead": ahead,
                        "behind": behind
                    }
                })
                logger.info(f"Synced remote_heads for {repo_name} from another device")
            except Exception as e:
                if "error" not in task_result:
                    task_result["error"] = {}
                task_result.update({"error": str(e)})

        elif action == "run_ci":
            start_time = time.time()
            event_name = params.get("event", "manual")
            runner_id = params.get("runner_id")  # optional: specific runner

            try:
                logger.info(f"Running CI/CD for {repo_name} → event: {event_name}")

                # Use your elite CI runner
                cmd = [
                    "venv/bin/python", "stargit_ci.py",
                    str(repo_path),
                    event_name
                ]

                result = subprocess.run(
                    cmd,
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=1800  # 30 min max
                )

                commit_sha = git_utils.get_current_commit_sha(repo_path)

                task_result.update({
                    "result": {
                        "status": "success" if result.returncode == 0 else "failed",
                        "args": result.args,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "duration_seconds": time.time() - start_time,
                        "commit_sha": commit_sha
                    }
                })

                print("runner task result:", json.dumps({
                    "args": result.args,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr
                }, indent=4), flush=True)

                if result.returncode == 0:
                    logger.info(f"CI/CD succeeded for {repo_name}")
                else:
                    logger.error(f"CI/CD failed for {repo_name}: {result.stderr[:500]}")

            except subprocess.TimeoutExpired:
                task_result.update({
                    "error": "CI/CD timed out after 30 minutes"
                })
                logger.error(f"CI/CD timeout for {repo_name}")
            except Exception as e:
                task_result.update({"error": str(e)})
                logger.exception(f"CI/CD crashed for {repo_name}")

        elif action == "create_repo":
            print("create_repo action event", flush=True)
            params = params or {}
            repo_name = params.get("repo_name")
            owner = params.get("owner", "user")
            description = params.get("description", "")
            default_branch = params.get("default_branch", "main")
            visibility = params.get("visibility", "private")
            bare = params.get("is_bare", False)
            immutable = params.get("immutable", True)
            init_readme = params.get("init_readme", False)
            init_gitignore = params.get("init_gitignore", False)
            init_license = params.get("init_license", False)
            gitignore_template = params.get("gitignore_template", "")

            try:
                if not repo_name:
                    raise ValueError("repo_name is required")

                print("REPO_BASE:", REPO_BASE, flush=True)

                STARGIT_WORK_ROOT = os.path.join(REPO_BASE, "_work")
                os.makedirs(STARGIT_WORK_ROOT, exist_ok=True)

                if bare:
                    repo_path = os.path.join(REPO_BASE, f"{repo_name}.git")
                else:
                    repo_path = os.path.join(REPO_BASE, f"{repo_name}")
                print("repo_path:", repo_path, flush=True)

                # Create repo directory
                os.makedirs(repo_path, exist_ok=False)

                # Initialize bare repository
                # Initialize bare or non-bare repo
                git_init_args = [GIT_EXECUTABLE, "init"]
                if bare:
                    git_init_args.insert(1, "--bare")
                git_init_args.append(repo_path)
                subprocess.run(git_init_args, check=True)

                # Set repository description (Git uses 'description', not description.txt)
                if description:
                    with open(os.path.join(repo_path, "description"), "w", encoding="utf-8") as f:
                        f.write(description)

                # Create initial commit if needed
                if init_readme or init_gitignore or init_license:
                    if bare:
                        # Use a temporary workdir inside the repo storage
                        workdir = os.path.join(STARGIT_WORK_ROOT, f"init-{repo_name}-{uuid.uuid4()}")
                        os.makedirs(workdir, exist_ok=False)
                    else:
                        # Non-bare repo: use the repo_path itself
                        workdir = repo_path
                    
                    # Initialize (non-bare) workdir if bare
                    if bare:
                        subprocess.run([GIT_EXECUTABLE, "init", workdir], check=True)

                    subprocess.run([GIT_EXECUTABLE, "-C", workdir, "checkout", "-b", default_branch], check=True)

                    if init_readme:
                        with open(os.path.join(workdir, "README.md"), "w") as f:
                            f.write(f"# {repo_name}\n\n{description}")
                    if init_gitignore and gitignore_template:
                        with open(os.path.join(workdir, ".gitignore"), "w") as f:
                            f.write(get_gitignore_template(gitignore_template))
                    if init_license:
                        with open(os.path.join(workdir, "LICENSE"), "w") as f:
                            f.write(get_mit_license())

                    subprocess.run([GIT_EXECUTABLE, "-C", workdir, "add", "."], check=True)
                    subprocess.run([
                        GIT_EXECUTABLE, "-C", workdir, "commit",
                        "-m", "Initial commit",
                        "--author", "StarGit <noreply@stargit.com>"
                    ], check=True)

                    if bare:
                        # Push/fetch into bare repo
                        subprocess.run([
                            GIT_EXECUTABLE, "-C", repo_path, "fetch", workdir, f"{default_branch}:{default_branch}"
                        ], check=True)
                        shutil.rmtree(workdir)  # cleanup temporary workdir

                task_result["result"] = {
                    "repo_uuid": str(uuid.uuid4()),
                    "repo_path": repo_path,
                    "is_bare": bare
                }

            except Exception as e:
                logger.exception("create_repo failed")
                task_result.update({"error": str(e)})

        else:
            logger.warning(f"Unknown action: {action}")
            results.append({"task_id": task['id'], "result": None, "error": f"Unknown action: {action}"})
            continue

        # === AUTO-REFRESH AHEADS AND BEHIND ON HEADS-CHANGING ACTIONS: Check if remote_heads changed ===

        if needs_heads_refresh or needs_status_refresh:
            remote_heads = git_utils.get_remote_heads(repo_path)

        if needs_heads_refresh:
            ahead, behind = git_utils.get_ahead_behind(repo_path)
            if "result" not in task_result:
                task_result["result"] = {}
            task_result["result"]["ahead"] = ahead
            task_result["result"]["behind"] = behind

            # Compare with what we had before this task
            old_remote_heads = task.get("previous_remote_heads") or {}

            if remote_heads != old_remote_heads:
                logger.info(f"Remote heads changed for {repo_name} → broadcasting to other devices")

                # Attach to result — this triggers broadcast on Stargit
                task_result["result"]["broadcast_remote_heads"] = {
                    "repo_name": repo_name,
                    "remote_heads": remote_heads
                }

        # === AUTO-REFRESH STATUS ON STATUS-CHANGING ACTIONS ===
        if needs_status_refresh:
            fresh_status, status_error = get_git_status_data(repo_path)
            
            if status_error:
                logger.warning(f"Failed to refresh status after {action}: {status_error}")
            else:
                # Attach fresh status — Stargit will update DB instantly
                if "result" not in task_result:
                    task_result["result"] = {}
                task_result["result"]["repo_status"] = fresh_status
                task_result["result"]["remote_heads"] = remote_heads  # ← This is gold
                logger.debug(f"Fresh status attached after {action}")

        results.append(task_result)
    
    logger.debug(f"Processed results: {results}")
    return results

# Poll function (reuses your access_token logic)
def poll_for_tasks(results=None):
    verbose = False
    if verbose:
        logger.debug("Polling for tasks")
    if not STARGIT_API_KEY:
        return []
    access_token = get_access_token()
    if not access_token or not SERVER_UUID:
        logger.error("No valid access token or server UUID for poll")
        return []
    payload = {
        "server_uuid": SERVER_UUID,
        "event_type": "poll",
        "timestamp": time.time(),
        "results": results or []
    }
    
    if verbose:
        logger.debug(f"Sending payload: {payload}")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    endpoint = POLL_ENDPOINT
    try:
        response = requests.post(endpoint, json=payload, headers=headers)
        
        if verbose:
            logger.debug(f"Response status: {response.status_code}, body: {response.text}")
        if response.status_code == 200:
            data = response.json()
            tasks = data.get('tasks', [])
            if verbose:
                logger.debug(f"Received {len(tasks)} tasks: {tasks}")
            return tasks
        elif response.status_code == 401:
            logger.warning("Token invalid; refreshing")
            tokens['access_token'] = None
            access_token = get_access_token()
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
                response = requests.post(endpoint, json=payload, headers=headers)
                logger.debug(f"Retry status: {response.status_code}, body: {response.text}")
                if response.status_code == 200:
                    data = response.json()
                    tasks = data.get('tasks', [])
                    logger.debug(f"Received {len(tasks)} tasks after retry: {tasks}")
                    return tasks
        logger.error(f"Poll failed: {response.text} (status: {response.status_code})")
        return []
    except Exception as e:
        logger.error(f"Error during poll: {str(e)}")
        return []
    
# Poll thread (separate from registration_thread for different interval)
def poll_thread():
    results = []  # Start with empty
    while True:
        #_pause_event.wait()

        tasks = poll_for_tasks(results)
        if tasks:
            results = process_tasks(tasks)
            # Immediately send results
            poll_for_tasks(results)  # Send results immediately by polling again with results
            results = []  # Clear after sending
        time.sleep(1.0)  # Every 5 seconds; adjust via env var if needed

# Start registration thread
if STARGIT_API_KEY:
    logger.info("Loaded STARBRIDGE_SERVER_UUID: %s", SERVER_UUID)
    threading.Thread(target=registration_thread, daemon=True).start()
    threading.Thread(target=poll_thread, daemon=True).start()
else:
    logger.info("No hearteat - hearbeat disabled", flush=True)

# Endpoins for web server querying status and configuration
# TODO : Secure these endpoints with authentication if exposed publicly
@app.route('/internal/stats', methods=['GET'])
def internal_stats():
    # Uptime
    uptime = time.time() - psutil.Process(os.getpid()).create_time()
    # Server hostname
    host_name = socket.gethostname()
    metrics = {
        'host_name': host_name,
        'uptime_seconds': uptime,
        'cpu_percent': psutil.cpu_percent(),
        'memory_percent': psutil.virtual_memory().percent,
        # Add more from your collect_server_metrics
    }
    return jsonify(metrics)

# TODO : Secure these endpoints with authentication if exposed publicly
@app.route('/internal/logs', methods=['GET'])
def internal_logs():
    log_path = 'starbridge.log'
    if os.path.exists(log_path):
        with open(log_path, 'r') as f:
            lines = f.readlines()[-100:]  # Last 100 lines
        return jsonify({'logs': ''.join(lines)})
    return jsonify({'logs': 'No logs found'})

@app.route('/health')
def health():
    """Simple health check endpoint"""

    frontend_port = os.getenv("ADMIN_PORT", "5002")
    frontend_url = f"http://127.0.0.1:{frontend_port}"

    return jsonify({
        "status": "healthy",
        "service": "StarGit",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": "1.0.0",  # todo: replace with your actual version
        "ports": {
            "backend": int(request.host.split(':')[1]) if ':' in request.host else 5001,
            "frontend": int(frontend_port),
            "frontend_url": frontend_url
        }
    }), 200


@app.route('/kill')
def kill_server():
    """Gracefully shut down the Flask server"""
    if request.remote_addr not in ["127.0.0.1", "::1"]:
        return jsonify({"error": "Access denied"}), 403

    try:
        requests.get(f"http://127.0.0.1:{ADMIN_PORT}/shutdown", timeout=2)
    except Exception as e:
        logger.warning(f"Shutdown request failed: {e}")

    def shutdown():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=shutdown, daemon=True).start()
    return jsonify({"status": "shutting_down"}), 200

@app.route('/pause', methods=['POST'])
def pause_server():
    if request.remote_addr not in ["127.0.0.1", "::1"]:
        return jsonify({"error": "Forbidden"}), 403
    _pause_event.set()
    logger.info("StarBridge PAUSED via API")
    return jsonify({"status": "paused"}), 200

@app.route('/resume', methods=['POST'])
def resume_server():
    if request.remote_addr not in ["127.0.0.1", "::1"]:
        return jsonify({"error": "Forbidden"}), 403
    _pause_event.clear()
    logger.info("StarBridge RESUMED via API")
    return jsonify({"status": "running"}), 200

@app.route('/status')
def server_status():
    return jsonify({
        "status": "paused" if _pause_event.is_set() else "running",
        "paused": _pause_event.is_set()
    }), 200

# Main entry point for running the web-server for monitoring and configuration frontend
ENABLE_FRONTEND = os.getenv('ENABLE_FRONTEND', 'true').lower() == 'true'
if ENABLE_FRONTEND:
    def start_frontend():
        subprocess.Popen(['python', 'frontend.py'])
    threading.Thread(target=start_frontend, daemon=True).start()
    logger.info(f"Frontend enabled and started on http://localhost:{ADMIN_PORT}")

# At the very end (after app is created)
def initialize_live_sync():
    def get_token():
        return get_access_token()  # your existing function

    watchdog_live_diff.start_live_sync(
        repositories=REPOSITORIES,
        server_uuid=SERVER_UUID,
        token_getter=get_token
    )

# Start it once
threading.Thread(target=initialize_live_sync, daemon=True).start()

if __name__ == '__main__':
    logger.info("Starting StarBridge server")
    if SSL_MODE and SSL_MODE == 'adhoc':
        logger.warning("Starting server in adhoc SSL mode")
        app.run(ssl_context='adhoc', host='0.0.0.0', port=STARBRIDGE_PORT)
    else:
        app.run(ssl_context=(CERT_PATH, KEY_PATH), host='0.0.0.0', port=STARBRIDGE_PORT)

logger.info("StarBridge server online")
