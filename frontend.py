from flask import Flask, render_template_string, request, redirect, url_for, jsonify, render_template
import os
import json
from dotenv import load_dotenv, dotenv_values
import psutil
import requests
import subprocess
import sys

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
    <a href="/endpoints">API Endpoints</a> | 
    <a href="/poll">Poll Features</a> | 
    <a href="/stats">Stats</a> | 
    <a href="/license">License</a>
</nav>
"""

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
        stats_resp = requests.get('https://127.0.0.1:5005/internal/stats', verify=False, timeout=5)
        metrics = stats_resp.json() if stats_resp.ok else {}
    except:
        metrics = {}

    # Fetch logs
    try:
        logs_resp = requests.get('https://127.0.0.1:5005/internal/logs', verify=False, timeout=5)
        logs = logs_resp.json().get('logs', 'No logs') if logs_resp.ok else 'Failed to fetch logs'
    except:
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

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=ADMIN_PORT)
