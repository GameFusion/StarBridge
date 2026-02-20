# watchdog_live_diff.py
import os
import time
import threading
import logging
import requests
import json
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import git_utils

# === SILENCE WATCHDOG NOISE FOREVER ===
logging.getLogger("watchdog").setLevel(logging.WARNING)
logging.getLogger("watchdog.observers").setLevel(logging.WARNING)
logging.getLogger("watchdog.observers.inotify_buffer").setLevel(logging.WARNING)

logger = logging.getLogger("StarBridge.Watchdog")


# === CONFIG ===
DEBOUNCE_SECONDS = 0.8
LIVE_UPDATE_ENDPOINT = "https://stargit.com/api/servers/live-update"

class LiveDiffWatcher(FileSystemEventHandler):
    def __init__(self, repo_path: str, repo_name: str, server_uuid: str, send_callback):
        self.repo_path = Path(repo_path).resolve()
        self.repo_name = repo_name
        self.server_uuid = server_uuid
        self.send_callback = send_callback
        self.last_trigger = 0
        self.debounce_seconds = 0.8
        self.last_sent_diff = None
        self.last_sent_hash = None

        # Auto-detect log file (for extra safety)
        self.log_file_path = None
        for h in logging.getLogger().handlers:
            if hasattr(h, 'baseFilename'):
                self.log_file_path = Path(h.baseFilename).resolve()
                break

        # === IGNORE COUNTER ===
        self.ignored_count = 0
        self.last_reported = 0
        self.report_every = 1000  # Whisper every 1000th ignored event

    def should_ignore(self, event_src_path: str) -> bool:
        try:
            path = Path(event_src_path).resolve()

            #print("should_ignore", path, flush=True)
            #return True

            # 1. Our own log file (nuclear safety)
            if self.log_file_path and path == self.log_file_path:
                return True

            # 2. Inside .git
            if '.git' in path.parts:
                return True

            # 3. Common noise
            ignored_dirs = {
                '.stargit', 'logs', '__pycache__', 'node_modules',
                '.venv', 'venv', 'build', 'dist', '.pytest_cache'
            }
            if any(part in ignored_dirs for part in path.parts):
                return True

            # 4. File patterns
            if path.suffix.lower() in {'.log', '.tmp', '.swp', '.pyc', '.pyo'}:
                self._increment_and_maybe_report(path, "temp/noise file")
                return True
            if path.name.startswith('.') or path.name.endswith('~'):
                return True

            print("******* FILE CHANGED ", path)
            return False
        except Exception as e:
            logger.debug(f"Ignore check failed: {e}")
            return True  # Be safe

    def on_any_event(self, event):
        if event.is_directory:
            return

        if self.should_ignore(event.src_path):
            return

        file_path = Path(event.src_path)

        # CRITICAL: Only trigger for tracked files
        if not git_utils.is_file_tracked(self.repo_path, file_path):
            print(f"> > > > skipping untracked file {file_path}")
            return
        
        now = time.monotonic()
        if now - self.last_trigger < self.debounce_seconds:
            return

        self.last_trigger = now

        # === Only send if diff actually changed ===
        try:
            current_diff = git_utils.get_diff(self.repo_path)
            current_diff_str = current_diff.get("diff", "")
            import hashlib
            current_hash = hashlib.md5(current_diff_str.encode('utf-8')).hexdigest()

            # FIXED: Only compare if we have sent something before
            if self.last_sent_hash is not None and current_hash == self.last_sent_hash:
                print(">> >> No diff change - skipping live update")
                return

            # Real change OR first time
            self.last_sent_diff = current_diff
            self.last_sent_hash = current_hash

            rel_path = Path(event.src_path).relative_to(self.repo_path)
            logger.info(f"Live diff changed -> {self.repo_name}/{rel_path}")
            print(f">>> LIVE UPDATE SENT: {self.repo_name}/{rel_path}", flush=True)
            self.send_callback(self.repo_path, self.repo_name)

        except Exception as e:
            logger.error(f"Failed to compute diff for live update: {e}")

        except Exception as e:
            logger.error(f"Failed to compute diff for live update: {e}")

    def _increment_and_maybe_report(self, path: Path, reason: str):
        """Thread-safe counter + occasional whisper"""
        self.ignored_count += 1

        if (self.ignored_count - self.last_reported) >= self.report_every:
            rel = path.relative_to(self.repo_path) if path.is_relative_to(self.repo_path) else path.name
            logger.info(
                f"Watchdog: Ignored {self.ignored_count} events in {self.repo_name} "
                f"(example: {rel} - {reason})"
            )
            self.last_reported = self.ignored_count

class LiveSyncManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance.observer = None
                    cls._instance.started = False
        return cls._instance

    def __init__(self):
        if hasattr(self, "initialized"):
            return
        self.initialized = True
        self.observer = Observer()

    def start_watching(self, repo_path: str, repo_name: str, server_uuid: str, token_getter):
        """Start watching one repo"""
        def send_update(repo_path, repo_name):

            #print(f"> > > > > > > > > > > > SEND UPDATE LIVE for repo {repo_name} at path {repo_path}", flush=True)
            
            try:
                
                diff = git_utils.get_diff(repo_path)
                #print("diff = ", diff, flush=True)
                ahead, behind = git_utils.get_ahead_behind(repo_path)

                payload = {
                    "server_uuid": server_uuid,
                    "event_type": "live_diff_update",
                    "repo_name": repo_name,
                    "live_diff": diff,
                    "ahead": ahead,
                    "behind": behind,
                    "timestamp": time.time()
                }

                access_token = token_getter()
                if not access_token:
                    print("No access token — aborting live update", flush=True)
                    return

                print(f"Sending to {LIVE_UPDATE_ENDPOINT} with token: {access_token[:20]}...", flush=True)

                ret = requests.post(
                    LIVE_UPDATE_ENDPOINT,
                    json=payload,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=8
                )

                #print(f"HTTP {ret.status_code} {ret.reason}", flush=True)
                try:
                    response_json = ret.json()
                    print("Response JSON:", json.dumps(response_json, indent=2), flush=True)
                except:
                    print("Response text (not JSON):", ret.text, flush=True)

                if ret.status_code == 403:
                    print("403 FORBIDDEN — Your token is missing scope 'servers:live-update'")
                    print("Fix: Regenerate API key with 'servers:live-update' scope")
                    
                print(f"- >> - >> - >> - >> - >> - >> - >> Live update sent for {repo_name}", flush=True)
                print(f"- >> - >> - >> - >> - >> - >> - >> Live update ret", ret, flush=True)

            except Exception as e:
                print(f"Live update failed for {name}: {e}")
                logger.error(f"Live update failed for {name}: {e}")

        handler = LiveDiffWatcher(repo_path, repo_name, server_uuid, send_update)
        self.observer.schedule(handler, repo_path, recursive=True)
        logger.info(f"Started live sync watcher: {repo_name}")

    def start_all(self, repositories, server_uuid, token_getter):
        """Start watching all repos at startup"""
        if self.started:
            return
        self.started = True

        for repo_path in repositories:
            repo_name = os.path.basename(repo_path)
            try:
                self.start_watching(repo_path, repo_name, server_uuid, token_getter)
            except Exception as e:
                logger.error(f"Failed to watch {repo_name}: {e}")

        self.observer.start()
        logger.info(f"LiveSyncManager: Watching {len(repositories)} repositories")

        # Keep thread alive
        threading.Thread(target=self._keep_alive, daemon=True).start()

    def _keep_alive(self):
        try:
            while True:
                time.sleep(1)
        except:
            pass

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()

# === Public API ===
def start_live_sync(repositories, server_uuid, token_getter):
    """Call this once at startup"""
    LiveSyncManager().start_all(repositories, server_uuid, token_getter)
