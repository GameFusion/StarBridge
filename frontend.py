from flask import Flask, request, redirect, url_for, render_template
import os
import json
import uuid
import secrets
from datetime import datetime, timezone
from dotenv import load_dotenv, dotenv_values
import psutil
import requests
import subprocess
import sys
import urllib3

app = Flask(__name__)

# Load configs
SETTINGS_PATH = 'settings.json'
ENV_PATH = '.env'
load_dotenv()

ADMIN_PORT = os.getenv('ADMIN_PORT', 5002)

# Navigation menu
NAV = """
<nav style="margin-bottom: 20px;">
    <a href="/">Config</a> | 
    <a href="/repos">Manage Repos</a> | 
    <a href="/repository">Repository</a> | 
    <a href="/endpoints">API Endpoints</a> | 
    <a href="/poll">Poll Features</a> | 
    <a href="/stats">Stats</a> | 
    <a href="/license">License</a>
</nav>
"""


def get_backend_base_url():
    backend_host = os.getenv("STARBRIDGE_HOST") or os.getenv("STARBHRIDGE_HOST") or "127.0.0.1"
    backend_port = str(os.getenv("STARBRIDGE_PORT", "5001")).strip()
    backend_scheme = (os.getenv("STARBRIDGE_SCHEME") or "").strip().lower()

    if backend_scheme not in ("http", "https"):
        ssl_mode = (os.getenv("SSL_MODE", "none") or "none").strip().lower()
        backend_scheme = "https" if ssl_mode in ("adhoc", "cert", "self-signed", "self_signed") else "http"

    return f"{backend_scheme}://{backend_host}:{backend_port}", backend_scheme == "https"


BACKEND_BASE_URL, BACKEND_IS_HTTPS = get_backend_base_url()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def backend_request(path, method="POST", payload=None, timeout=12):
    headers = {}
    api_key = (os.getenv("STARBRIDGE_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key

    candidate_urls = [f"{BACKEND_BASE_URL}{path}"]
    if BACKEND_BASE_URL.startswith("http://"):
        candidate_urls.append(f"{BACKEND_BASE_URL.replace('http://', 'https://', 1)}{path}")
    elif BACKEND_BASE_URL.startswith("https://"):
        candidate_urls.append(f"{BACKEND_BASE_URL.replace('https://', 'http://', 1)}{path}")

    response = None
    last_request_error = None

    for url in candidate_urls:
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                json=payload,
                headers=headers,
                timeout=timeout,
                verify=not url.startswith("https://")
            )
            last_request_error = None
            break
        except requests.RequestException as exc:
            last_request_error = exc
            continue

    if response is None:
        return None, f"{method.upper()} {path} failed: {last_request_error}"

    response_data = {}
    try:
        response_data = response.json() if response.content else {}
    except ValueError:
        response_data = {"raw": response.text}

    if 200 <= response.status_code < 300:
        return response_data, None

    error = response_data.get("error") if isinstance(response_data, dict) else None
    details = response_data.get("details") if isinstance(response_data, dict) else None
    message = error or response.reason or "backend error"
    if details:
        message = f"{message} ({details})"
    return None, f"{response.status_code}: {message}"

# Helper to load settings
def load_settings():
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, 'r') as f:
            return json.load(f)
    return {}

# Helper to save settings
def save_settings(settings):
    with open(SETTINGS_PATH, 'w') as f:
        json.dump(settings, f, indent=4)

# Helper to load .env as dict (non-secrets only for edit)
def load_env():
    return dotenv_values(ENV_PATH)

# Helper to save .env (only allowed keys)
ALLOWED_ENV_KEYS = ['GIT_VERBOSE', 'PUSH_MODE', 'POLL_MODE', 'SSL_MODE', 'ENABLE_FRONTEND']  # No secrets
def save_env(updates):
    env = load_env()
    for key in ALLOWED_ENV_KEYS:
        if key in updates:
            env[key] = updates[key]
    with open(ENV_PATH, 'w') as f:
        for k, v in env.items():
            f.write(f"{k}={v}\n")

@app.route('/', methods=['GET', 'POST'])
def config():
    settings = load_settings()
    env = load_env()
    if request.method == 'POST':
        # Update settings
        settings['git_executable'] = request.form.get('git_executable', settings['git_executable'])
        settings['ssl']['cert_path'] = request.form.get('cert_path', settings['ssl']['cert_path'])
        settings['ssl']['key_path'] = request.form.get('key_path', settings['ssl']['key_path'])
        save_settings(settings)
        
        # Update env
        env_updates = {
            'GIT_VERBOSE': request.form.get('git_verbose', 'false'),
            'PUSH_MODE': request.form.get('push_mode', 'false'),
            'SSL_MODE': request.form.get('ssl_mode', 'none'),
            'ENABLE_FRONTEND': request.form.get('enable_frontend', 'true')
        }
        save_env(env_updates)
        
        # Regenerate secrets if requested
        if 'regenerate_api_key' in request.form:
            env['STARBRIDGE_API_KEY'] = secrets.token_hex(32)
            save_env({'STARBRIDGE_API_KEY': env['STARBRIDGE_API_KEY']})
        if 'regenerate_uuid' in request.form:
            env['STARBRIDGE_SERVER_UUID'] = str(uuid.uuid4())
            save_env({'STARBRIDGE_SERVER_UUID': env['STARBRIDGE_SERVER_UUID']})
        
        return redirect(url_for('config'))
    
    # Mask secrets
    masked_env = {k: (v[:4] + '...' if k in ['STARBRIDGE_API_KEY', 'STARBRIDGE_SERVER_UUID'] else v) for k, v in env.items()}
    
    return render_template('config.html', settings=settings, env=env, masked_env=masked_env, nav=NAV)

@app.route('/repos', methods=['GET', 'POST'])
def manage_repos():
    settings = load_settings()
    repos = settings.get('repositories', [])

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            new_repo = request.form.get('new_repo')
            if new_repo and new_repo not in repos:
                repos.append(new_repo)
        elif action == 'remove':
            repo_to_remove = request.form.get('repo')
            if repo_to_remove in repos:
                repos.remove(repo_to_remove)
        settings['repositories'] = repos
        save_settings(settings)
        return redirect(url_for('manage_repos'))

    return render_template(
        'repos.html',
        repos=repos,
        nav=NAV
    )


def _extract_remote_names(remotes_payload):
    names = []
    for remote in (remotes_payload or {}).get("remotes", []):
        name = remote.get("name")
        if name and name not in names:
            names.append(name)
    return names


def _extract_local_branches(branches_payload):
    names = []
    for branch in (branches_payload or {}).get("local_branches", []):
        name = branch.get("name")
        if name and name not in names:
            names.append(name)
    return names


def _preferred_branch(status_payload, branch_names):
    branch = ((status_payload or {}).get("branch") or "").strip()
    if branch and branch != "HEAD":
        return branch
    if branch_names:
        return branch_names[0]
    return "HEAD"


@app.route('/repository', methods=['GET', 'POST'])
def repository_page():
    settings = load_settings()
    repos = settings.get('repositories', []) or []

    selected_repo = request.values.get('repo_path', '').strip()
    if not selected_repo and repos:
        selected_repo = repos[0]

    selected_remote = request.values.get('remote', 'origin').strip() or "origin"
    selected_branch = request.values.get('branch', '').strip()
    pull_mode = request.values.get('pull_mode', 'ff-only').strip() or "ff-only"
    force_push = (request.values.get('force_push', '').strip().lower() in ('1', 'true', 'on', 'yes'))

    action_message = None
    action_error = None
    action_output = None

    if request.method == 'POST' and selected_repo:
        action = request.form.get('action', '').strip()

        if action == "pull":
            pull_mode_map = {
                "ff-only": "--ff-only",
                "rebase": "--rebase",
                "merge": "--no-rebase",
            }
            payload = {
                "repo_path": selected_repo,
                "pull_mode": pull_mode_map.get(pull_mode, "--ff-only"),
            }
            response_data, request_error = backend_request('/api/pull', payload=payload, method='POST')
            if request_error:
                action_error = f"Pull failed: {request_error}"
            else:
                action_message = "Pull completed."
                action_output = (response_data or {}).get("output") or (response_data or {}).get("message")

        elif action == "push":
            branch_for_push = selected_branch or "HEAD"
            payload = {
                "repo_path": selected_repo,
                "remote": selected_remote,
                "branch": branch_for_push,
                "force": force_push
            }
            response_data, request_error = backend_request('/api/push', payload=payload, method='POST')
            if request_error:
                action_error = f"Push failed: {request_error}"
            else:
                action_message = "Force push completed." if force_push else "Push completed."
                action_output = (response_data or {}).get("status")

        elif action == "refresh":
            action_message = "Repository details refreshed."

    status_payload, status_error = ({}, None)
    remotes_payload, remotes_error = ({}, None)
    branches_payload, branches_error = ({}, None)
    diff_payload, diff_error = ({}, None)
    history_payload, history_error = ({}, None)
    health_payload, health_error = ({}, None)

    remote_names = []
    branch_names = []
    history_branch = "HEAD"
    diff_text = ""
    diff_preview = ""
    diff_preview_truncated = False
    latest_poll_utc = None

    if selected_repo:
        status_payload, status_error = backend_request('/api/status', payload={"repo_path": selected_repo}, method='POST')
        remotes_payload, remotes_error = backend_request('/api/remotes', payload={"repo_path": selected_repo}, method='POST')
        branches_payload, branches_error = backend_request('/api/branch', payload={"repo_path": selected_repo}, method='POST')

        remote_names = _extract_remote_names(remotes_payload)
        branch_names = _extract_local_branches(branches_payload)

        if selected_remote not in remote_names and remote_names:
            selected_remote = remote_names[0]

        if not selected_branch:
            selected_branch = _preferred_branch(status_payload, branch_names)
        elif selected_branch not in branch_names and selected_branch != "HEAD":
            branch_names.append(selected_branch)

        history_branch = selected_branch or _preferred_branch(status_payload, branch_names)

        diff_payload, diff_error = backend_request('/api/diff', payload={"repo_path": selected_repo}, method='POST')
        history_payload, history_error = backend_request(
            '/api/revwalk',
            payload={"repo_path": selected_repo, "branch": history_branch},
            method='POST'
        )
        health_payload, health_error = backend_request('/health', method='GET')

        diff_text = (diff_payload or {}).get("diff", "") or ""
        diff_preview_limit = 20000
        if len(diff_text) > diff_preview_limit:
            diff_preview = diff_text[:diff_preview_limit]
            diff_preview_truncated = True
        else:
            diff_preview = diff_text

        polling = (health_payload or {}).get("polling", {}) or {}
        last_success = polling.get("last_success")
        if last_success:
            latest_poll_utc = datetime.fromtimestamp(last_success, timezone.utc).isoformat()

    commits = (history_payload or {}).get("commits", []) or []
    brief_history = commits[:20]

    return render_template(
        'repository.html',
        nav=NAV,
        repos=repos,
        selected_repo=selected_repo,
        selected_remote=selected_remote,
        selected_branch=selected_branch,
        pull_mode=pull_mode,
        force_push=force_push,
        remote_names=remote_names,
        branch_names=branch_names,
        action_message=action_message,
        action_error=action_error,
        action_output=action_output,
        status_payload=status_payload or {},
        status_error=status_error,
        remotes_payload=remotes_payload or {},
        remotes_error=remotes_error,
        branches_error=branches_error,
        diff_error=diff_error,
        diff_preview=diff_preview,
        diff_text_len=len(diff_text),
        diff_preview_truncated=diff_preview_truncated,
        diff_info=(diff_payload or {}).get("diff_info", {}),
        history_error=history_error,
        history_branch=history_branch,
        brief_history=brief_history,
        health_payload=health_payload or {},
        health_error=health_error,
        latest_poll_utc=latest_poll_utc,
        refreshed_at=datetime.now(timezone.utc).isoformat(),
        backend_base_url=BACKEND_BASE_URL
    )

@app.route('/endpoints')
def endpoints():
    endpoint_list = [
        {'path': '/api/refs', 'method': 'POST', 'desc': 'Get local and remote refs for a repo.'},
        {'path': '/api/add', 'method': 'POST', 'desc': 'Add a file to the index.'},
        {'path': '/api/branch', 'method': 'POST', 'desc': 'Get local and remote branches.'},
        {'path': '/api/remotes', 'method': 'POST', 'desc': 'Get remotes.'},
        {'path': '/api/revwalk', 'method': 'POST', 'desc': 'Get commit history.'},
        {'path': '/api/diff', 'method': 'POST', 'desc': 'Get diffs.'},
    ]
    return render_template(
        'api_endpoints.html',
        endpoint_list=endpoint_list,
        nav=NAV
    )

@app.route('/poll')
def poll_features():
    features = [
        'get_file: Fetch file content at a ref (text or base64 binary).',
        'get_file_history: Get commit history for a file.',
        # Add more features as needed
    ]
    return render_template(
        'poll_features.html',
        features=features,
        nav=NAV
    )
@app.route('/stats', methods=['GET', 'POST'])
def stats():
    if request.method == 'POST' and 'restart' in request.form:
        # Restart app.py (kill current process, restart)
        pid = os.getpid()
        subprocess.Popen([sys.executable, 'app.py'])  # Restart
        psutil.Process(pid).terminate()  # Kill current
        return 'Restarting...'

    # Fetch metrics
    try:
        metrics, metrics_error = backend_request('/internal/stats', method='GET', timeout=5)
        if metrics_error:
            metrics = {}
    except Exception:
        metrics = {}

    # Fetch logs
    try:
        logs_payload, logs_error = backend_request('/internal/logs', method='GET', timeout=5)
        if logs_error:
            logs = f'Failed to fetch logs: {logs_error}'
        else:
            logs = (logs_payload or {}).get('logs', 'No logs')
    except Exception:
        logs = 'Log endpoint unreachable'

    uptime_h = int(metrics.get('uptime_seconds', 0) // 3600)
    uptime_m = int((metrics.get('uptime_seconds', 0) % 3600) // 60)

    return render_template(
        'stats.html',
        uptime_h=uptime_h,
        uptime_m=uptime_m,
        cpu_percent=metrics.get('cpu_percent', 'N/A'),
        memory_percent=metrics.get('memory_percent', 'N/A'),
        logs=logs,
        nav=NAV
    )

@app.route('/license')
def license_page():
    license_path = 'LICENSE'
    if os.path.exists(license_path):
        with open(license_path, 'r') as f:
            content = f.read()
    else:
        content = 'No LICENSE file found. Add one to the root directory.'
    return render_template(
        'license.html',
        content=content,
        nav=NAV
    )


@app.route('/shutdown', methods=['GET'])
def shutdown():
    # Local-only shutdown endpoint used by StarBridge backend/launcher.
    if request.remote_addr not in ["127.0.0.1", "::1"]:
        return "Forbidden", 403

    shutdown_fn = request.environ.get('werkzeug.server.shutdown')
    if shutdown_fn:
        shutdown_fn()
        return "Frontend shutting down", 200

    # Fallback for non-werkzeug execution context.
    os._exit(0)
    return "Frontend shutting down", 200

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=ADMIN_PORT)
