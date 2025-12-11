from flask import Flask, render_template_string, request, redirect, url_for, jsonify
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

# Ultra-dark theme CSS (inspired by stargit.com: black bg, neon green accents)
DARK_THEME_CSS = """
body { background-color: #000; color: #fff; font-family: monospace; margin: 0; padding: 20px; }
a { color: #0f0; text-decoration: none; } a:hover { text-decoration: underline; }
h1, h2 { color: #0f0; }
input, select, textarea, button { background: #111; color: #fff; border: 1px solid #0f0; padding: 8px; }
button { cursor: pointer; } button:hover { background: #0f0; color: #000; }
ul { list-style: none; padding: 0; }
li { margin: 10px 0; }
form { max-width: 800px; }
pre { background: #111; padding: 10px; overflow: auto; }
"""

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
ALLOWED_ENV_KEYS = ['GIT_VERBOSE', 'PUSH_MODE', 'SSL_MODE', 'ENABLE_FRONTEND']  # No secrets
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
    
    html = f"""
    <html><head><style>{DARK_THEME_CSS}</style></head><body>
    {NAV}
    <h1>StarBridge Configuration</h1>
    <form method="POST">
        <h2>Settings (settings.json)</h2>
        <label>Git Executable: <input name="git_executable" value="{settings.get('git_executable', '')}"></label><br>
        <label>SSL Cert Path: <input name="cert_path" value="{settings['ssl'].get('cert_path', '')}"></label><br>
        <label>SSL Key Path: <input name="key_path" value="{settings['ssl'].get('key_path', '')}"></label><br>
        
        <h2>Environment (.env - Non-Secrets)</h2>
        <label>GIT_VERBOSE: <select name="git_verbose"><option value="true" {'selected' if env.get('GIT_VERBOSE', 'false') == 'true' else ''}>true</option><option value="false" {'selected' if env.get('GIT_VERBOSE', 'false') == 'false' else ''}>false</option></select></label><br>
        <label>PUSH_MODE: <select name="push_mode"><option value="true" {'selected' if env.get('PUSH_MODE', 'false') == 'true' else ''}>true</option><option value="false" {'selected' if env.get('PUSH_MODE', 'false') == 'false' else ''}>false</option></select></label><br>
        <label>SSL_MODE: <input name="ssl_mode" value="{env.get('SSL_MODE', 'none')}"></label><br>
        <label>ENABLE_FRONTEND: <select name="enable_frontend"><option value="true" {'selected' if env.get('ENABLE_FRONTEND', 'true') == 'true' else ''}>true</option><option value="false" {'selected' if env.get('ENABLE_FRONTEND', 'true') == 'false' else ''}>false</option></select></label><br>
        
        <h2>Secrets (Masked - Regenerate if Needed)</h2>
        <p>API Key: {masked_env.get('STARBRIDGE_API_KEY', 'N/A')} <button type="submit" name="regenerate_api_key" value="1">Regenerate</button></p>
        <p>Server UUID: {masked_env.get('STARBRIDGE_SERVER_UUID', 'N/A')} <button type="submit" name="regenerate_uuid" value="1">Regenerate</button></p>
        
        <button type="submit">Save Changes</button>
    </form>
    </body></html>
    """
    return render_template_string(html)

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
    
    html = f"""
    <html><head><style>{DARK_THEME_CSS}</style></head><body>
    {NAV}
    <h1>Manage Repositories</h1>
    <ul>
    {"".join(f'<li>{repo} <form method="POST" style="display:inline;"><input type="hidden" name="action" value="remove"><input type="hidden" name="repo" value="{repo}"><button>Remove</button></form></li>' for repo in repos)}
    </ul>
    <form method="POST">
        <input type="hidden" name="action" value="add">
        <label>Add Repo Path: <input name="new_repo"></label>
        <button type="submit">Add</button>
    </form>
    </body></html>
    """
    return render_template_string(html)

@app.route('/endpoints')
def endpoints():
    # Static list based on your app.py
    endpoint_list = [
        {'path': '/api/refs', 'method': 'POST', 'desc': 'Get local and remote refs for a repo.'},
        {'path': '/api/add', 'method': 'POST', 'desc': 'Add a file to the index.'},
        {'path': '/api/branch', 'method': 'POST', 'desc': 'Get local and remote branches.'},
        {'path': '/api/remotes', 'method': 'POST', 'desc': 'Get remotes.'},
        {'path': '/api/revwalk', 'method': 'POST', 'desc': 'Get commit history.'},
        {'path': '/api/diff', 'method': 'POST', 'desc': 'Get diffs.'},
        # Add more from your app.py
    ]
    html = f"""
    <html><head><style>{DARK_THEME_CSS}</style></head><body>
    {NAV}
    <h1>API Endpoints (Pull Mode)</h1>
    <ul>
    {"".join(f'<li><strong>{e["path"]}</strong> ({e["method"]}): {e["desc"]}</li>' for e in endpoint_list)}
    </ul>
    </body></html>
    """
    return render_template_string(html)

@app.route('/poll')
def poll_features():
    # Based on your poll_thread and process_tasks
    features = [
        'get_file: Fetch file content at a ref (text or base64 binary).',
        'get_file_history: Get commit history for a file.',
        # Add more
    ]
    html = f"""
    <html><head><style>{DARK_THEME_CSS}</style></head><body>
    {NAV}
    <h1>Secure Poll Mode Features</h1>
    <p>StarBridge polls Stargit for tasks every 1s and processes them securely.</p>
    <ul>
    {"".join(f'<li>{f}</li>' for f in features)}
    </ul>
    </body></html>
    """
    return render_template_string(html)

@app.route('/stats', methods=['GET', 'POST'])
def stats():
    if request.method == 'POST' and 'restart' in request.form:
        # Restart app.py (kill current process, restart)
        pid = os.getpid()
        subprocess.Popen([sys.executable, 'app.py'])  # Restart
        psutil.Process(pid).terminate()  # Kill current
        return 'Restarting...'

    # Fetch from internal API
    try:
        stats_resp = requests.get('http://localhost:5001/internal/stats')
        metrics = stats_resp.json() if stats_resp.status_code == 200 else {}
        logs_resp = requests.get('http://localhost:5001/internal/logs')
        logs = logs_resp.json().get('logs', '') if logs_resp.status_code == 200 else 'No logs'
    except:
        metrics = {}
        logs = 'API server not responding'

    html = f"""
    <html><head><style>{DARK_THEME_CSS}</style></head><body>
    {NAV}
    <h1>Stats & Monitoring</h1>
    <p>Uptime: {metrics.get('uptime_seconds', 0) // 3600}h { (metrics.get('uptime_seconds', 0) % 3600) // 60}m</p>
    <p>CPU: {metrics.get('cpu_percent', 'N/A')}% | Memory: {metrics.get('memory_percent', 'N/A')}%</p>
    <form method="POST"><button name="restart" value="1">Restart Server</button></form>
    <h2>Logs (Last 100 lines)</h2>
    <pre>{logs}</pre>
    <h2>Best Practices</h2>
    <ul>
        <li>Backup .env regularly.</li>
        <li>Monitor logs for errors.</li>
        <li>Use SSL for production.</li>
    </ul>
    </body></html>
    """
    return render_template_string(html)

@app.route('/license')
def license_page():
    license_path = 'LICENSE'
    if os.path.exists(license_path):
        with open(license_path, 'r') as f:
            content = f.read()
    else:
        content = 'No LICENSE file found. Add one to the root directory.'
    html = f"""
    <html><head><style>{DARK_THEME_CSS}</style></head><body>
    {NAV}
    <h1>License</h1>
    <pre>{content}</pre>
    </body></html>
    """
    return render_template_string(html)

@app.route('/shutdown')
def shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func: func()
    return 'Frontend shutting down...'

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=ADMIN_PORT)
