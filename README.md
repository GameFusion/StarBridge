# StarBridge: Remote Git Repository Management & CI/CD

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](https://choosealicense.com/licenses/mit/)
[![Python Version](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![GitHub Issues](https://img.shields.io/github/issues/StargitStudio/StarBridge)](https://github.com/StargitStudio/StarBridge/issues)
[![GitHub Stars](https://img.shields.io/github/stars/StargitStudio/StarBridge)](https://github.com/StargitStudio/StarBridge/stargazers)

StarBridge is a versatile, open-source service for remote Git repository management and CI/CD, designed to empower developers with API-driven control over their repositories. Built with Python and Flask, StarBridge runs on Linux, Windows, and macOS, offering a standalone RESTful API to interact with multiple Git repositories programmatically. Whether you’re automating commits, diffs, or deployments, StarBridge provides a secure, efficient way to manage repositories via API calls, making it ideal for scripts, automation, or custom integrations.

When paired with [StarGit](https://stargit.com), StarBridge transforms into a powerful backend for graphical repository management. Think of StarGit as a user-friendly desktop client that connects to your StarBridge instance, enabling you to visualize and interact with multiple repositories remotely, providing an at-a-glance overview of source code and asset states. This combination simplifies complex workflows, from syncing media-heavy repos to orchestrating CI/CD pipelines.

Developed by **Stargit Studio AB** in Stockholm, Sweden, StarBridge is a cornerstone of our mission to streamline software development with API-driven automation and AI-assisted CI/CD. Its standalone API mode empowers developers to manage repositories programmatically, while StarGit’s optional graphical interface makes multi-repository management accessible to all. Whether you’re an indie developer or an enterprise team, StarBridge offers a robust, free solution to enhance your development process.

## Features

- **Standalone API-Driven Git Management**: Control multiple Git repositories via a secure REST API, supporting commits, branches, diffs, pushes, pulls, and status checks.
- **Graphical Multi-Repository Management**: With StarGit, visualize and manage repository states (code, assets) across servers, with an intuitive desktop interface.
- **CI/CD Automation**: Facilitates automated deployments and edits, extensible for custom workflows.
- **Cross-Platform Support**: Runs as a lightweight service on Linux, Windows, and macOS.
- **Secure Authentication**: Uses API keys or stargit.com token validation for access, with premium monitoring options.
- **Flexible SSL**: Supports HTTPS with custom certificates or ad-hoc mode for development.
- **Server Registration**: Opt-in registration with stargit.com for health monitoring and future CI/CD automation.
- **Open-Source & Free**: MIT-licensed, community-driven, and cost-free for all users.

StarBridge’s API-driven design makes it a novel tool for developers seeking programmatic control over repositories, while its integration with StarGit offers a graphical alternative for streamlined management. Deploy it standalone or with StarGit to elevate your development workflows.

## Prerequisites

Before installing StarBridge, ensure your system meets these requirements:
- **Operating System**: Linux (Ubuntu/CentOS recommended), Windows, or macOS.
- **Python**: 3.8 or higher.
- **Git**: Installed and accessible (e.g., `/usr/bin/git` on Linux, `git` on Windows/macOS).
- **Dependencies**: Virtual environment recommended (`venv`).
- **SSL Certificates**: Optional for production (Let’s Encrypt or self-signed).
- **Firewall**: Open port 5001 (or 443 with a reverse proxy).
- **Optional**: PostgreSQL for custom extensions; not required for core functionality.

## Installation

Follow these steps to set up StarBridge on your system.

### 1. Clone the Repository
```bash
git clone https://github.com/StargitStudio/StarBridge.git
cd StarBridge
```

### 2. Set Up a Virtual Environment
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3. Install Dependencies
Install required packages:
```bash
pip install -r requirements.txt
```
**requirements.txt**:
```
Flask
GitPython
gunicorn
requests
python-dotenv
pyOpenSSL
psutil
```

### 4. Configure Settings
- Copy the example settings file:
  ```bash
  cp example-settings.json settings.json
  ```
- Edit `settings.json`:
  ```json
  {
      "git_executable": "/usr/bin/git",
      "repositories": [
          "/path/to/repo1",
          "/path/to/repo2"
      ],
      "ssl": {
          "cert_path": "/path/to/cert.pem",
          "key_path": "/path/to/key.pem"
      }
  }
  ```
  - `git_executable`: Path to Git binary (e.g., `C:\Program Files\Git\bin\git.exe` on Windows).
  - `repositories`: List of trusted repository paths.
  - `ssl`: Paths to SSL certificate and key (see SSL Setup).

- Generate `.env` for secrets:
  ```bash
  python setup.py
  ```
  This creates `.env` with a secure `STARBRIDGE_API_KEY`. Edit `.env` for additional options:
  ```plaintext
  STARBRIDGE_API_KEY=your_generated_key_here
  SSL_MODE=adhoc  # For development with self-signed certs
  ENABLE_STARGIT_REGISTRATION=true
  STARGIT_API_KEY=your_stargit_key_here  # From stargit.com
  STARBRIDGE_SERVER_ID=unique_id_here  # Optional UUID
  ```

### 5. SSL Setup
For HTTPS (required for StarGit):
- **Development (Ad-Hoc)**:
  ```bash
  mkdir certs
  openssl req -x509 -newkey rsa:4096 -nodes -out certs/starbridge-cert.pem -keyout certs/starbridge-key.pem -days 365 -subj "/CN=localhost"
  chown your_user:your_group certs/*  # Linux only
  chmod 640 certs/*
  ```
  Set `SSL_MODE=adhoc` in `.env`.
- **Production**: Use Let’s Encrypt:
  ```bash
  sudo certbot certonly --standalone -d <server_ip> --non-interactive --agree-tos --email your_email@example.com
  ```
  Update `settings.json` with `/etc/letsencrypt/live/<server_ip>/fullchain.pem` and `privkey.pem`.

### 6. Run in Development Mode
Test StarBridge:
```bash
python app.py
```
Access at `https://localhost:5001` or `https://<server_ip>:5001` (use `-k` with curl for self-signed certs):
```bash
curl -k -H "x-api-key: $(grep STARBRIDGE_API_KEY .env | cut -d'=' -f2)" -X POST -d '{"repo_path": "/path/to/repo", "branch": "main"}' https://<server_ip>:5001/api/status
```

## Production Deployment
For production, use Gunicorn with systemd (Linux) or a service manager (Windows/macOS).

### Linux (Systemd)
Create `/etc/systemd/system/starbridge.service`:
```ini
[Unit]
Description=StarBridge Service
After=network.target

[Service]
User=your_user  # e.g., gamefusion
Group=your_group  # e.g., users
WorkingDirectory=/path/to/StarBridge
Environment="PATH=/path/to/StarBridge/venv/bin:/usr/bin"
ExecStart=/path/to/StarBridge/venv/bin/gunicorn -b 0.0.0.0:5001 --timeout 100 --certfile=/path/to/certs/starbridge-cert.pem --keyfile=/path/to/certs/starbridge-key.pem app:app

[Install]
WantedBy=multi-user.target
```
Start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable starbridge
sudo systemctl start starbridge
sudo systemctl status starbridge
```
Monitor logs: `tail -f starbridge.log`.

### Windows/macOS
Run Gunicorn manually or use a service wrapper (e.g., NSSM on Windows, launchd on macOS):
```bash
source venv/bin/activate  # Windows: venv\Scripts\activate
gunicorn -b 0.0.0.0:5001 --timeout 100 --certfile=certs/starbridge-cert.pem --keyfile=certs/starbridge-key.pem app:app
```

### Firewall Setup
Open port 5001 (or 443 for Nginx):
```bash
sudo firewall-cmd --add-port=5001/tcp --permanent  # Linux firewalld
sudo firewall-cmd --reload
```
For cloud servers, update security groups to allow TCP 5001.

### Optional: Nginx Reverse Proxy
For production scalability:
```nginx
server {
    listen 443 ssl;
    server_name <server_ip>;
    ssl_certificate /path/to/certs/starbridge-cert.pem;
    ssl_certificate_key /path/to/certs/starbridge-key.pem;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Save to `/etc/nginx/conf.d/starbridge.conf` (Linux), then:
```bash
sudo systemctl restart nginx
```

## API Endpoints
StarBridge’s REST API enables programmatic Git management:
- **POST /api/refs**: List local/remote refs.
- **POST /api/branch**: List branches.
- **POST /api/remotes**: List remotes.
- **POST /api/revwalk**: Get commit history.
- **POST /api/add**: Stage files.
- **POST /api/commit**: Create commits.
- **POST /api/diff**: Get diffs.
- **POST /api/status**: Get repository status.
- **POST /api/push/start**, **/api/push/object**, **/api/push/ref**, **/api/push/end**: Manage push sessions.
- **POST /api/pull/start**, **GET /api/pull/object**, **POST /api/pull/ref**, **POST /api/pull/end**: Manage pull sessions.

Example:
```bash
curl -k -H "x-api-key: your_key" -X POST -d '{"repo_path": "/path/to/repo", "branch": "main"}' https://<server_ip>:5001/api/branch
```

## Integration with StarGit
StarBridge shines as a standalone API service, but it’s designed to work seamlessly with [StarGit](https://stargit.com), a free desktop client for graphical Git management. StarGit connects to your StarBridge instance to:
- Visualize multiple repositories’ states (code, assets) remotely.
- Perform Git operations with an intuitive interface.
- Leverage AI features like commit message generation and security alerts.

Download StarGit from [stargit.com](https://stargit.com) and configure it with your StarBridge IP and API key. For advanced monitoring, enable `ENABLE_STARGIT_REGISTRATION=true` in `.env` to register your instance with stargit.com’s premium dashboard, tracking metrics like uptime and CPU usage, with future support for batch CI/CD automation.

## Security Considerations
- **API Keys**: Keep `.env` and `settings.json` out of Git (`.gitignore`).
- **SSL**: Use Let’s Encrypt in production; ad-hoc mode is for development.
- **Repositories**: Restrict to trusted paths in `settings.json`.
- **Permissions**: Run as a non-root user with minimal privileges.
- **Registration**: Opt-in only, preserving privacy for standalone use.

## Contributing
We welcome contributions! To contribute:
1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/my-feature`).
3. Test locally (`python app.py`).
4. Commit with clear messages (`git commit -m "Add feature X"`).
5. Submit a pull request to `main`.

Report issues at [GitHub Issues](https://github.com/StargitStudio/StarBridge/issues).

## License
StarBridge is licensed under the [MIT License](LICENSE), free to use, modify, and distribute.

## About Stargit Studio AB
Stargit Studio AB, based in Stockholm, Sweden, is committed to advancing software development through API-driven automation and AI-assisted tools. StarBridge, paired with StarGit, simplifies multi-repository management and CI/CD, empowering developers to build efficiently. Visit [stargit.com](https://stargit.com) for StarGit downloads, community engagement, or premium features like server monitoring. Contact support@stargit.com for inquiries.

StarBridge offers a new way to manage Git repositories with APIs, enhanced by StarGit’s graphical interface. Deploy it to streamline your workflows and explore the future of automated development!
