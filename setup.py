import os
import secrets
import uuid
import json
from pathlib import Path

def generate_api_key(length=32):
    """Generate a secure random API key."""
    return secrets.token_hex(length)

def generate_server_uuid():
    """Generate a persistent UUID for this server instance."""
    return str(uuid.uuid4())

def generate_env_file():
    # Auto-generate .env if missing
    env_path = Path('.env')
    if not env_path.exists():
        # Generate credentials
        api_key = generate_api_key()
        server_uuid = generate_server_uuid()
        with open(env_path, 'w') as f:
            f.write(f"STARBRIDGE_API_KEY={api_key}\n")
            f.write(f"STARBRIDGE_SERVER_UUID={server_uuid}\n")
            f.write("GIT_VERBOSE=false\n")
            f.write("PUSH_MODE=false\n")
            f.write("SSL_MODE=adhoc\n") 
            f.write("ENABLE_FRONTEND=true\n")  # Default enabled
        print("Generated .env with API key and server UUID.")
        return api_key, server_uuid
    return None, None

def generate_settings_json():
    settings_path = Path('settings.json')
    if not settings_path.exists():
        default_settings = {
            "git_executable": "/usr/bin/git",
            "repositories": [],
            "ssl": {
                "cert_path": "",
                "key_path": ""
            }
        }
        with open(settings_path, 'w') as f:
            json.dump(default_settings, f, indent=4)
        print("Generated default settings.json.")

def ensure_gitignore_includes_env():
    """Ensure .env is in .gitignore to prevent accidental commits."""
    gitignore_path = Path('.gitignore')
    env_entry = '.env\n'

    if gitignore_path.exists():
        with open(gitignore_path, 'r') as f:
            content = f.read()
        if env_entry not in content:
            with open(gitignore_path, 'a') as f:
                f.write(env_entry)
            print("Added '.env' to .gitignore to prevent accidental commits.")
    else:
        with open(gitignore_path, 'w') as f:
            f.write(env_entry)
        print("Created .gitignore with '.env' entry.")

def setup_starbridge():
    """Set up StarBridge by creating a .env file with a secure API key and server UUID."""

    api_key, server_uuid = generate_env_file()
    generate_settings_json()
    # Ensure .env is protected from git
    ensure_gitignore_includes_env()

    # Print summary
    print("\n🚀 StarBridge setup complete!")
    if api_key and server_uuid:
        print(f"🔑 API key: {api_key}")
        print(f"🆔 Server UUID: {server_uuid}")
        print("\n⚠️  Store these values securely! They uniquely identify this server instance.")
    
    print("\nNext steps:")
    print("1. Edit 'settings.json' to configure your repository paths and SSL settings.")
    print("2. Ensure your SSL certificates are correctly set up as specified in settings.json.")
    print("3. Run the application: `python app.py` or enable the starbridge.service systemd unit.")
    print("4. The API key will be used for authentication, and the server UUID will identify this instance to Stargit.")


if __name__ == '__main__':
    print("Starting StarBridge command-line setup...")
    setup_starbridge()
else:
    print("Starting StarBridge auto-setup...")
    generate_env_file
    generate_settings_json()
    ensure_gitignore_includes_env()