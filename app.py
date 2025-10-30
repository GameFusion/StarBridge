
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
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('starbridge.log', maxBytes=1000000, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('StarBridge')

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Load settings from the JSON file
settings_file_path = os.path.join(os.path.dirname(__file__), 'settings.json')
with open(settings_file_path) as settings_file:
    settings = json.load(settings_file)

GIT_EXECUTABLE = settings.get("git_executable")
REPOSITORIES = settings["repositories"]
CERT_PATH = settings['ssl']['cert_path']
KEY_PATH = settings['ssl']['key_path']

STARGIT_API_KEY = os.getenv('STARGIT_API_KEY', '')

# Hardcoded endpoints (override with STARGIT_API_URL for testing)
STARGIT_API_URL = os.getenv('STARGIT_API_URL', 'https://stargit.com')
AUTH_ENDPOINT = f"{STARGIT_API_URL}/api/auth/token"
REGISTER_ENDPOINT = f"{STARGIT_API_URL}/api/servers/register"
HEARTBEAT_ENDPOINT = f"{STARGIT_API_URL}/api/servers/heartbeat"

# Add this near the top, after loading env
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
    'api_key_uuid': None  # Store APIKey.uuid as server_id
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
    logger.debug("Running git command: %s in path: %s", command, path)
    try:
        result = subprocess.run(command, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
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
    Get detailed Git status for the given repository path.
    Returns:
        (status_data, error)
        where `error` is None if success, otherwise a dict with details.
    """
    try:
        # Run `git status --porcelain` to detect changes
        result = subprocess.run(
            [git_executable, "status", "--porcelain", "-b"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        output = result.stdout.strip().splitlines()

        summary = {
            "added": [],
            "modified": [],
            "deleted": [],
            "renamed": [],
            "untracked": []
        }

        branch_line = output[0] if output else ""
        has_conflict = False
        has_pending_changes = False
        only_untracked = False

        for line in output[1:]:  # skip branch line
            if not line.strip():
                continue

            parts = line.split(maxsplit=2)
            if len(parts) < 2:
                continue

            status_code = parts[0]
            file_path = parts[1]

            # Detect merge conflicts
            if "U" in status_code:  
                has_conflict = True

            if status_code == "A":
                summary["added"].append(file_path)
            elif status_code == "M":
                summary["modified"].append(file_path)
            elif status_code == "D":
                summary["deleted"].append(file_path)
            elif status_code.startswith("R") and "->" in file_path:
                old, new = file_path.split("->", 1)
                summary["renamed"].append({"from": old.strip(), "to": new.strip()})
            elif status_code == "??":
                summary["untracked"].append(file_path)

        has_pending_changes = any(summary[k] for k in summary if k != "untracked")
        only_untracked = bool(summary["untracked"]) and not has_pending_changes

        # Determine action_summary
        if has_conflict:
            action_summary = "Merge conflict"
        elif has_pending_changes:
            action_summary = "Pending changes"
        elif only_untracked:
            action_summary = "Untracked files"
        else:
            # Check if branch is ahead/behind remote
            ahead = re.search(r"\[ahead (\d+)\]", branch_line)
            behind = re.search(r"\[behind (\d+)\]", branch_line)

            if behind:
                action_summary = "Ready to pull"
            elif ahead:
                action_summary = "Ready to push"
            else:
                action_summary = "Up to date"

        # Build action_message as before
        if has_pending_changes:
            details = [f"{len(v)} {k}" for k, v in summary.items() if v]
            action_message = f"Pending changes detected: {', '.join(details)}. Please commit or stash your changes."
        elif only_untracked:
            action_message = f"{len(summary['untracked'])} untracked file(s) detected. You may want to add them."
        elif has_conflict:
            action_message = "Merge conflicts detected. Resolve them before continuing."
        else:
            action_message = "No changes detected. Working directory is clean."

        status_data = {
            "summary": summary,
            "action_message": action_message,
            "action_summary": action_summary
        }
        return status_data, None

    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else str(e)
        logger.error("Git error retrieving status for %s: %s", repo_path, error_message)
        return None, {"type": "GitError", "message": error_message}

    except Exception as e:
        logger.error("Unexpected error retrieving git status for %s: %s", repo_path, str(e))
        return None, {"type": "Exception", "message": str(e)}

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
        stats = {
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
    if tokens['access_token'] and tokens['expires_at'] and datetime.utcnow() < tokens['expires_at'] - timedelta(seconds=60):
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
    logger.debug("Using API key (masked): %s...", STARGIT_API_KEY[:8])
    try:
        headers = {
            "Authorization": f"Bearer {STARGIT_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "scopes": "servers:register servers:heartbeat"
            # No metadata in initial request
        }
        response = requests.post(AUTH_ENDPOINT, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            tokens['access_token'] = data['access_token']
            tokens['refresh_token'] = data.get('refresh_token')
            tokens['expires_at'] = datetime.utcnow() + timedelta(hours=1)
            tokens['api_key_uuid'] = data.get('api_key_uuid')
            logger.info("New access token fetched successfully, api_key_uuid: %s", tokens['api_key_uuid'])
            return tokens['access_token']
        else:
            logger.error("Failed to fetch token from %s: %s (status: %d, key: %s...)", AUTH_ENDPOINT, response.text, response.status_code, STARGIT_API_KEY[:8])
            return None
    except Exception as e:
        logger.error("Error fetching token from %s: %s (key: %s...)", AUTH_ENDPOINT, str(e), STARGIT_API_KEY[:8])
        return None

# Add this new function to collect detailed repository information
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
        process = subprocess.Popen(command, stdout=PIPE, stderr=PIPE)
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
        
        servers["repos"].append(repo)
    
    return servers

def register_with_stargit(event_type='heartbeat'):
    """Send registration or heartbeat to stargit.com using token."""
    logger.debug(f"Registering with stargit.com, event_type={event_type}")
    if not STARGIT_API_KEY:
        logger.info("Stargit registration disabled in .env")
        return
    access_token = get_access_token()
    if not access_token or not tokens['api_key_uuid']:
        logger.error("No valid access token or api_key_uuid; registration aborted (key: %s...)", STARGIT_API_KEY[:8] if STARGIT_API_KEY else "None")
        return
    else:
        logger.info("Valid access token recieved %s: ", access_token)

    try:
        metrics = collect_server_metrics() if event_type == 'heartbeat' else {}
        payload = {
            "server_id": tokens['api_key_uuid'],
            "event_type": event_type,
            "status": "online",
            "metrics": metrics,
            "timestamp": time.time(),
            "ip_address": requests.get('https://api.ipify.org').text if event_type == 'online' else None
        }
        # Include detailed repo info only in heartbeats if PUSH_MODE is enabled
        if event_type == 'heartbeat' and PUSH_MODE:
            payload["repositories"] = collect_repo_details()
            logger.info("repositories:\n%s", json.dumps(payload["repositories"], indent=4, default=str))
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
    if STARGIT_API_KEY:
        register_with_stargit(event_type='online')  # Initial registration
        while True:
            register_with_stargit(event_type='heartbeat')  # Periodic heartbeat
            time.sleep(300)  # 5 minutes

# Start registration thread
if STARGIT_API_KEY:
    threading.Thread(target=registration_thread, daemon=True).start()

# Main entry point for running the server
if __name__ == '__main__':
    logger.info("Starting StarBridge server")
    if SSL_MODE and SSL_MODE == 'adhoc':
        logger.warning("Starting server in adhoc SSL mode")
        app.run(ssl_context='adhoc', host='0.0.0.0', port=5001)
    else:
        app.run(ssl_context=(CERT_PATH, KEY_PATH), host='0.0.0.0', port=5001)

logger.info("StarBridge server online")
