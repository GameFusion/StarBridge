import os
import secrets
import uuid
from pathlib import Path

def generate_api_key(length=32):
    """Generate a secure random API key."""
    return secrets.token_hex(length)

def generate_server_uuid():
    """Generate a persistent UUID for this server instance."""
    return str(uuid.uuid4())

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
    env_path = Path('.env')

    if env_path.exists():
        print("⚠️  A .env file already exists. To regenerate the API key or server UUID, delete .env and rerun this script.")
        return

    # Generate credentials
    api_key = generate_api_key()
    server_uuid = generate_server_uuid()

    # Write to .env
    with open(env_path, 'w') as f:
        f.write(f"STARBRIDGE_API_KEY={api_key}\n")
        f.write(f"STARBRIDGE_SERVER_UUID={server_uuid}\n")

    # Ensure .env is protected from git
    ensure_gitignore_includes_env()

    # Print summary
    print("\n🚀 StarBridge setup complete!")
    print(f"🔑 API key: {api_key}")
    print(f"🆔 Server UUID: {server_uuid}")
    print("\n⚠️  Store these values securely! They uniquely identify this server instance.")
    print("\nNext steps:")
    print("1. Copy 'example-settings.json' to 'settings.json' and configure your repository paths and SSL settings.")
    print("2. Ensure your SSL certificates are correctly set up as specified in settings.json.")
    print("3. Run the application: `python app.py` or enable the starbridge.service systemd unit.")
    print("4. The API key will be used for authentication, and the server UUID will identify this instance to Stargit.")


if __name__ == '__main__':
    print("Starting StarBridge setup...")
    setup_starbridge()
