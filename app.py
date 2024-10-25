
from flask import Flask, request, jsonify, abort
import os
#import git
#git.refresh('/usr/bin/git')  # Replace with the actual path to git if different
from flask import Flask, request, jsonify, Response
from subprocess import Popen, PIPE
import subprocess
import os
import re
import uuid
import time
import json

app = Flask(__name__)

# Load settings from the JSON file
settings_file_path = os.path.join(os.path.dirname(__file__), 'settings.json')
with open(settings_file_path) as settings_file:
    settings = json.load(settings_file)

GIT_EXECUTABLE = settings.get("git_executable")

# Set the repository path and repositories
REPO_PATH = settings["repo_path"]
REPOSITORIES = settings["repositories"]

# Initialize repository object
#repo = git.Repo(REPO_PATH)

# HTTPS configuration (add your certificate and key paths)
CERT_PATH = settings['ssl']['cert_path']
KEY_PATH = settings['ssl']['key_path']

# Example API key storage (in a real-world app, use a database)
VALID_API_KEYS = {"user123": "your_secure_api_key_here"}

# API key check that aborts if the key is invalid (no return to parent function)
def check_api_key():
    if request.headers.get('x-api-key') not in VALID_API_KEYS.values():
        print("Invalid key", flush=True)
        abort(401, description="Unauthorized access, invalid API key")

def validate_session(lock_path, session_id):
    """
    Validate a push session by checking the lock file and session ID.

    Parameters:
    - lock_path: Path to the lock file (e.g., local_lock_path).
    - session_id: The session ID to validate.

    Returns:
    - Tuple: (is_valid, error_message). If valid, returns (True, None), else returns (False, "Error message").
    """

    # Check if the lock file exists
    if not os.path.exists(lock_path):
        error_msg = f"Missing lock for valid push session on {lock_path}."
        return False, error_msg

    # Read the lock file and check the session ID
    try:
        with open(lock_path, 'r') as lock_file:
            lock_data = json.load(lock_file)
            # Ensure the session_id in the lock matches the provided session_id
            if lock_data.get('session_id') != session_id:
                error_msg = f"Invalid session ID {session_id} for lock at {lock_path}."
                return False, error_msg
    except Exception as e:
        # If there is any issue reading or parsing the lock file, return an error
        error_msg = f"Error validating session: {str(e)}"
        return False, error_msg

    # If all checks pass, return valid
    return True, None

def run_git_command(path, command):
    """Utility function to run a git command and return the output."""
    try:
        result = subprocess.run(command, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Error: {e.stderr.strip()}"

@app.route('/api/revwalk', methods=['POST'])
def rev_walk():
    print("/api/revwalk", flush=True)

    check_api_key()  # Your function to verify the API key

    data = request.json
    branch = data.get('branch')
    path = data.get('repo_path')
    mod = data.get('mod', None)  # Optional

    print("path", path, flush=True)
    if not branch:
        branch = run_git_command(REPO_PATH, [GIT_EXECUTABLE, "rev-parse", "--abbrev-ref", "HEAD"])

    if not path:
        branch 
        #return jsonify({"error": "Branch is required"}), 400

    # Path to repo (adjust if multiple repos)
    if path not in REPOSITORIES:
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

    # Example return structure
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

    print("commits", commit_list, flush=True)
    return jsonify({"commits": commit_list}), 200

@app.route('/api/diff', methods=['POST'])
def diff():
    """
    API to get the diff of uncommitted changes, a single commit, or between two commits.
    If a single commit is provided, also return the commit details (message, author, email, date, parents).
    """
    print("/api/diff", flush=True)
    check_api_key()
    data = request.json
    path = data.get('repo_path')
    commit = data.get('commit')
    commit1 = data.get('commit1')
    commit2 = data.get('commit2')

    # Define a size limit for the diff output (e.g., 5 MB)
    DIFF_CUTOFF_SIZE = 1 * 1024 * 1024  # 5 MB in bytes

    if path not in REPOSITORIES:
        return jsonify({"error": f"Repository path '{path}' not found in registered repositories"}), 400


    try:
        if not os.path.isdir(path):
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
            raise Exception(f"Error running git diff: {result.stderr}")

        diff_output = result.stdout
        original_diff_size = len(diff_output)

        # Check if the diff exceeds the cutoff size and truncate if necessary
        truncated = False
        if original_diff_size > DIFF_CUTOFF_SIZE:
            diff_output = diff_output[:DIFF_CUTOFF_SIZE]
            truncated = True

        print(f"diff len: {original_diff_size} -> {len(diff_output)}, truncated: {truncated} ", flush=True)

        commit_details = None
        if commit:
            # Get the commit details (message, author, email, date, and parents)
            commit_details_command = [
                GIT_EXECUTABLE, "-C", path, "show", "--no-patch", "--format=%B%n%an%n%ae%n%ad%n%P", commit
            ]
            result_message = subprocess.run(commit_details_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if result_message.returncode != 0:
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
        return jsonify({"error": str(e)}), 500

@app.route('/api/push', methods=['POST'])
def push():
    """
    API to push changes to a remote repository.
    Expects JSON with the repo_path and optional branch.
    """
    print("/api/push", flush=True)
    check_api_key()  # Function to verify the API key

    data = request.json
    repo_path = data.get('repo_path')
    branch = data.get('branch', 'master')  # Default to 'main' if branch is not specified

    print("data", data, flush=True)

    if repo_path not in REPOSITORIES:
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    print("found repository", flush=True)

    try:
        print("try to push", flush=True)
        # Prepare the git push command
        git_push_command = [GIT_EXECUTABLE, "-C", repo_path, "push", "origin", branch]

        # Run the git push command
        result = subprocess.run(git_push_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        print("result from push:", result, flush=True)

        if result.returncode != 0:
            raise Exception(f"Error pushing changes: {result.stderr.strip()}")

        # Return success response
        return jsonify({
            "status": "Push successful",
            "branch": branch
        }), 200

    except Exception as e:
        print("Push exception generated:", str(e), flush=True)
        return jsonify({
            "error": "Push command failed",
            "details": str(e)
        }), 500

@app.route('/api/add-remote', methods=['POST'])
def add_remote():
    """
    API to add a new remote to a Git repository.
    Example: git remote add origin git@stargit.gamefusion.io:GameFusion/Python/StarBridge.git
    """
    print("/api/add-remote", flush=True)
    check_api_key()

    data = request.json
    remote_name = data.get('remote_name', 'origin')
    remote_url = data.get('remote_url')
    repo_path = data.get('repo_path')

    if not remote_url:
        return jsonify({"error": "Remote URL is required"}), 400

    if not repo_path or repo_path not in REPOSITORIES:
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
            raise Exception(f"Error adding remote: {result.stderr}")

        # Return success response
        return jsonify({
            "status": "Remote added successfully",
            "remote_name": remote_name,
            "remote_url": remote_url
        }), 200

    except Exception as e:
        # Handle errors during remote add
        return jsonify({
            "error": str(e)
        }), 500

@app.route('/api/commit', methods=['POST'])
def commit():
    """
    API to stage changes and commit with the provided message.
    Returns the new commit ID on success.
    """
    print("/api/commit", flush=True)
    check_api_key()

    data = request.json

    name = data.get('name')
    email = data.get('email')
    path = data.get('repo_path')
    commit_message = data.get('message', 'Default commit message')

    print("name", name, flush=True)
    print("email", email, flush=True)

    if path not in REPOSITORIES:
        return jsonify({"error": f"Repository path '{path}' not found in registered repositories"}), 400

    print("using path", path)

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
        return jsonify({
            "error": "Git command failed",
            "details": e.stderr.decode() if e.stderr else str(e)
        }), 500


@app.route('/api/pull', methods=['POST'])
def pull():
    """
    API to pull changes from remote
    """
    check_api_key()
    try:
        origin = repo.remotes.origin
        pull_info = origin.pull()
        return jsonify({"status": "Pull successful", "info": str(pull_info)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/local-path', methods=['POST'])
def repo_path():
    """
    API to pull changes from remote
    """
    check_api_key()
    try:
        return jsonify({"status": "success", "local_path": str(REPO_PATH)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def extract_branch_from_error(error_message):
    # Regular expression to extract the branch name inside the single quotes

    match = re.search(r"your current branch '(.+?)'", error_message)
    if match:
        return match.group(1)
    return None

def get_current_branch_or_default(repo_path):
    print("get_current_branch_or_default", flush=True)
    """
    Returns the current branch name if the repository has commits.
    Otherwise, returns the default branch name to be used after the first commit.
    """
   # First, check if there are any commits in the repository
    log_output = run_git_command(repo_path, [GIT_EXECUTABLE, "log", "-1"])  # This will throw an error if there are no commits
    print("git log", log_output, flush=True)

    if "does not have any commits yet" in log_output:
        # Handle the case where the branch has no commits
        print("processing first commit...", flush=True)
        branch = extract_branch_from_error(log_output)
        print("found branch", flush=True)
        return {
            'status_message': f"Branch '{branch}' has no commits yet.",
            'branch': branch
            }
        #return f"Branch '{branch}' has no commits yet."

    # If commits exist, get the current branch name
    branch = run_git_command(repo_path, [GIT_EXECUTABLE, "rev-parse", "--abbrev-ref", "HEAD"])

    return {'branch':branch.strip(), 'status_message':''}



@app.route('/api/repositories', methods=['GET'])
def list_repositories():
    """
    API to list repositories with their name, local path, status, branch, and remote URL.
    """

    print("/api/repositories", flush=True)
    check_api_key()
    try:
        repositories = []
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
                "local_path": local_path,
                "status": status,
                "action_status": action_status,
                "branch": branch['branch'],
                "status_message": branch['status_message'],
                "remote": remote_url
            })

        # Construct the response
        response = {
            "repositories": repositories
        }

        return jsonify(response), 200

    except Exception as e:
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
                        print(f"Excluding file based on pattern '{pattern}': {relative_path}")
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

@app.route('/api/objects', methods=['POST'])
def list_git_objects():

    print("/api/objects", flush=True)
    check_api_key()

    data = request.json
    repo_path = data.get('repo_path')
    if repo_path not in REPOSITORIES:
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    objects_path = repo_path+'/.git/objects'  # Update this to point to your repository's .git/objects
    objects = list_directory_content(objects_path, data)
    return jsonify(objects)

@app.route('/api/refs', methods=['POST'])
def list_git_refs():

    print("/api/refs", flush=True)
    check_api_key()

    data = request.json
    repo_path = data.get('repo_path')
    print("repo_path", repo_path, flush=True)
    if repo_path not in REPOSITORIES:
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
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Construct the path to the object or ref file
    object_path = os.path.join(repo_path, '.git', sub_path, relative_path)  # Include relative path

    print("downloading object_path", object_path)

    # Read the specified object file
    if not os.path.isfile(object_path):
        return jsonify({"error": f"File not found at '{object_path}'"}), 404

    try:
        with open(object_path, 'rb') as file:
            file.seek(file_offset)  # Move the file pointer to the specified offset

            # Read the specified number of bytes or the entire file if max_bytes is None
            data = file.read(max_bytes) if max_bytes is not None else file.read()

            if not data:
                return jsonify({"error": "No data read from the specified offset"}), 404

            # Return the raw bytes and the size
            response = Response(data, mimetype='application/octet-stream')
            response.headers['Content-Length'] = str(len(data))
            return response
    except Exception as e:
        print("error exceptions", str(e))
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/object', methods=['POST'])
def download_object():
    print('/api/download/object', flush=True)
    check_api_key()
    return download_file(request.json, 'objects')

@app.route('/api/download/ref', methods=['POST'])
def download_ref():
    print('/api/download/ref', flush=True)
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
def create_lock(lock_path, client_id, session_id):
    # Prepare lock content
    lock_content = {
        "creation_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
        "last_update": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
        "client_id": client_id,
        "session_id": session_id
    }

    # Write the lock content to a .lock file
    try:
        with open(lock_path, 'w') as lock_file:
            json.dump(lock_content, lock_file)
        print("Created lock for push", lock_path, flush=True)
        return True
    except Exception as e:
        print("Failed to create lock for push", lock_path, flush=True)
        print(f"Failed to create lock at {lock_path}: {e}")
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
    print("/api/push/start", flush=True)
    check_api_key()
    data = request.json
    repo_path = data.get('repo_path')
    branch = data.get('branch')
    print("repo_path", repo_path)
    print("branch", branch)
    client_id = data.get('client_id')  # Client ID or generate a random one
    print("client_id", client_id)
    session_id = str(uuid.uuid4())  # Generate a unique session ID
    print("session_id", session_id)

    # Get the client head commit and remote commit
    client_head_commit = data.get('head_commit')
    client_remote_commit = data.get('remote_commit')

    print("client_head_commit", client_head_commit)
    print("client_remote_commit", client_remote_commit)

    # Get the current branch HEAD commit
    local_head_commit = get_commit_hash(repo_path, f'.git/refs/heads/{branch}')
    print("local_head_commit", local_head_commit)
    if local_head_commit is None:
        return jsonify({"error": f"Local branch '{branch}' does not exist."}), 404

    # Verify that the local head commit and client remote commit are the same
    if client_remote_commit != local_head_commit:
        return jsonify({"error": "Your local branch is out of date. Please pull the latest changes before pushing."}), 409

    # Verify if the client is up to date
    print(f"Client head commit: {client_remote_commit}, Local head commit: {local_head_commit}")
    if client_head_commit == local_head_commit:
        return jsonify({"error": "Already up to date. Nothing to pull."}), 409


    # Validate the repository
    if repo_path not in REPOSITORIES:
        return jsonify({"error": f"Repository '{repo_name}' not found"}), 400

    # Define lock paths
    local_lock_path = os.path.join(repo_path, '.git/refs/heads/push.lock')
    remote_lock_path = os.path.join(repo_path, '.git/refs/remotes/push.lock')

    # Check for existing locks
    if lock_exists(local_lock_path) or lock_exists(remote_lock_path):
        return jsonify({"error": "A push is already in progress. Lock file exists."}), 409

    # Create local lock (if local refs exist)
    local_heads_path = os.path.join(repo_path, '.git/refs/heads')
    if os.path.exists(local_heads_path):
        if not create_lock(local_lock_path, client_id, session_id):
            return jsonify({"error": "Failed to create local lock"}), 500

    # Create remote lock (if remote refs exist)
    remote_heads_path = os.path.join(repo_path, '.git/refs/remotes')
    if os.path.exists(remote_heads_path):
        if not create_lock(remote_lock_path, client_id, session_id):
            return jsonify({"error": "Failed to create remote lock"}), 500

    print("Initiated push with session id;", session_id)
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
        print(f"Failed to validate lock at {lock_path}: {e}")
        return False

# Helper function to remove the lock file
def remove_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
            print(f"Removed lock at {lock_path}")
            return True
        return False
    except Exception as e:
        print(f"Failed to remove lock at {lock_path}: {e}")
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
    # Check if the repository is bare or non-bare

    if is_bare_repository(repo_path):
        return {"reset_message": "No reset needed for bare repository."}, 200

    git_dir = os.path.join(repo_path, ".git")

    # If non-bare, check if the branch is the current branch in the working directory
    head_file_path = os.path.join(git_dir, "HEAD")
    if not os.path.exists(head_file_path):
        print("HEAD file not found")
        return {"error": "HEAD file not found, unable to reset"}, 404

    # Determine the current branch (if not bare)
    with open(head_file_path, "r") as f:
        head_content = f.read().strip()

    # Example format of HEAD: "ref: refs/heads/master"
    if head_content.startswith("ref: "):
        current_branch = head_content.split("/")[-1]  # Extract branch name from HEAD
        print(f"Current branch: {current_branch}")
    else:
        current_branch = None
        print(f"HEAD is detached, current commit is {head_content}")

    # Only reset if this is a non-bare repo and the branch matches the current branch
    if current_branch and branch == current_branch:
        try:
            print(f"Resetting branch {branch} in non-bare repository at {repo_path} to match the latest pushed commit.")

            # Perform git reset --hard using the git binary
            result = subprocess.run(['git', 'reset', '--hard'], cwd=repo_path, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"Error during git reset --hard: {result.stderr}")
                return {"error": f"Failed to reset branch: {result.stderr}"}, 500

            print(f"Branch {branch} successfully reset --hard.")
            return {"message": f"Push completed. Branch {branch} reset --hard to the latest commit."}, 200

        except Exception as e:
            print(f"Exception during git reset --hard: {e}")
            return {"error": f"Failed to reset branch: {str(e)}"}, 500

    else:
        # No reset needed (bare repo or branch does not match current branch)
        print(f"Push No reset performed (requested branch {branch} not checked out).")
        return {"message": "Push No reset performed (requested branch {branch} not checked out)."}, 200


# API to stop push and remove locks
@app.route('/api/push/end', methods=['POST'])
def stop_push():
    print("/api/push/end", flush=True)
    check_api_key()
    data = request.json
    repo_path = data.get('repo_path')
    session_id = data.get('session_id')  # Session ID to validate
    branch = data.get('branch')

    # Validate the repository
    if repo_path not in REPOSITORIES:
        return jsonify({"error": f"Repository '{repo_name}' not found"}), 400

    # Define lock paths
    local_lock_path = os.path.join(repo_path, '.git/refs/heads/push.lock')
    remote_lock_path = os.path.join(repo_path, '.git/refs/remotes/push.lock')

    # Validate and remove the local lock if it exists

    if lock_exists(local_lock_path):
        if not validate_lock(local_lock_path, session_id):
            print(f"error: Invalid session ID {session_id} for local lock")
            return jsonify({"error": "Invalid session ID for local lock"}), 403
        if not remove_lock(local_lock_path):
            print(f"error: Failed to remove local lock file {local_lock_path}")
            return jsonify({"error": "Failed to remove local lock"}), 500

    # Validate and remove the remote lock if it exists
    if lock_exists(remote_lock_path):
        if not validate_lock(remote_lock_path, session_id):
            print(f"error: Invalid session ID {session_id} for remote lock")
            return jsonify({"error": "Invalid session ID for remote lock"}), 403
        if not remove_lock(remote_lock_path):
            print(f"error: Failed to remove remote lock file {local_lock_path}")
            return jsonify({"error": "Failed to remove remote lock"}), 500


     # Attempt to reset the branch if needed
    reset_response, reset_status = reset_branch(repo_path, branch)

    # If there's an error from resetting the branch, include it in the final response
    if reset_status != 200:
        return jsonify({
            "message": "Push completed with reset errors.",
            "session_id": session_id,
            "reset_error": reset_response["error"]
        }), reset_status

    # If both locks are removed successfully
    return jsonify({
        "message": "Push completed and locks removed successfully",
        "session_id": session_id,
        "reset_message": reset_response.get("message")
    }), 200



@app.route('/api/push/object', methods=['POST'])
def push_object():
    print("/api/push/object", flush=True)

    # Verify API key
    check_api_key()

    #print("headers", request.headers)

    repo_path = request.headers.get('Repo-Path')
    print("repo_path", repo_path)

    # Validate repository
    if repo_path not in REPOSITORIES:
        return jsonify({"error": f"Repository path '{repo_path}' not found in registered repositories"}), 400

    # Verify session ID
    session_id = request.headers.get('Session-ID')
    print("session_id", session_id)
    if not session_id:
        return jsonify({"error": "Session ID is required"}), 400

    # Define lock paths
    local_lock_path = os.path.join(repo_path, '.git/refs/heads/push.lock')

    # Check for existing locks
    if not lock_exists(local_lock_path):
        # Return error message if the lock for a valid push session is missing
        return jsonify({"error": f"Missing lock for valid push session on {local_lock_path}."}), 404

    if not validate_lock(local_lock_path, session_id):
        print(f"error: Invalid session ID {session_id} for local lock")
        return jsonify({"error": "Invalid session ID for local lock"}), 403

    # Get the file path from the header
    file_path = request.headers.get('File-Path')
    if not file_path:
        return jsonify({"error": "File path is required"}), 400

    # Read the binary data from the request
    binary_data = request.data

    # Save the object to a file
    object_file_path = os.path.join(repo_path, '.git/objects', file_path)

    print("Recieving file path", object_file_path)
    #return jsonify({"message": "Object pushed successfully", "file_path": object_file_path}), 200

    try:
        # Create the directory if it doesn't exist
        os.makedirs(os.path.dirname(object_file_path), exist_ok=True)

        # Write the binary data to a file
        with open(object_file_path, 'wb') as obj_file:
            obj_file.write(binary_data)

        print(f"Object saved to {object_file_path}")

        return jsonify({"message": "Object pushed successfully", "file_path": object_file_path}), 200

    except Exception as e:
        print(f"Error saving object: {e}")
        return jsonify({"error": "Failed to save the object"}), 500

@app.route('/api/push/ref', methods=['POST'])
def update_ref():
    print("/api/push/ref", flush=True)
    check_api_key()

    # Get data from request
    data = request.json
    commit_id = data.get('commit_id')  # The commit ID to set
    repo_path = data.get('repo_path')   # The repository path
    session_id = data.get('session_id')  # Validate the session ID
    branch = data.get('branch')
    print("branch", branch)
    # Validate session
    # Define lock paths
    local_lock_path = os.path.join(repo_path, '.git/refs/heads/push.lock')
    is_valid, error_message = validate_session(local_lock_path, session_id)
    if not is_valid:
        print(f"error: {error_message}")
        return jsonify({"error": error_message}), 403 if "Invalid session" in error_message else 404

    # Define the path to heads/master
    heads_master_path = os.path.join(repo_path, f'.git/refs/heads/{branch}')

    # Check if the heads/master file exists
    if not os.path.exists(heads_master_path):
        return jsonify({"error": "heads/master not found"}), 404

    # Update heads/master with the new commit ID
    try:
        # Read the current commit ID from heads/{branch}
        with open(heads_master_path, 'r') as f:
            current_commit_id = f.read().strip()

        # Check if the current commit ID is already up to date
        if current_commit_id == commit_id:
            print(f"{branch} is already up to date with commit {commit_id}")
            return jsonify({"message": f"{branch} is already up to date", "commit_id": commit_id}), 200

        with open(heads_master_path, 'w') as f:
            f.write(commit_id + '\n')
        print(f"Updated {branch} from {current_commit_id} to {commit_id}")

        return jsonify({
            "message": f"Reference updated successfully for {branch}",
            "from_commit_id": current_commit_id,
            "to_commit_id": commit_id
        }), 200

    except Exception as e:
        print(f"Error updating heads/master: {e}")
        return jsonify({"error": "Failed to update reference"}), 500

# Ensure to add the necessary logic to verify API keys and maintain your repository list

# Main entry point for running the server
if __name__ == '__main__':
    print("CERT_PATH", CERT_PATH, flush=True)
    print("KEY_PATH", KEY_PATH, flush=True)
    app.run(ssl_context=(CERT_PATH, KEY_PATH), host='0.0.0.0', port=5001)


