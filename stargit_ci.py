# stargit_ci.py — The Most Elite CI/CD Runner on Earth
import yaml
import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime

import subprocess
from typing import Dict, Optional

import subprocess
import threading
from typing import Dict
import sys

def stream_pipe(pipe, buffer: list):
    for line in iter(pipe.readline, ''):
        buffer.append(line)
        print(line, end='')

def run_command(command: str) -> Dict[str, any]:
    """
    Run a shell command reliably, stream stdout and stderr concurrently,
    detect errors, and stop the runner if the command fails.
    """
    try:
        process = subprocess.Popen(
            ["bash", "-c", "set -e; " + command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        stdout_lines = []
        stderr_lines = []

        # Start threads to read stdout and stderr concurrently
        t_out = threading.Thread(target=stream_pipe, args=(process.stdout, stdout_lines))
        t_err = threading.Thread(target=stream_pipe, args=(process.stderr, stderr_lines))
        t_out.start()
        t_err.start()

        # Wait for process to finish and threads to complete
        process.wait()
        t_out.join()
        t_err.join()

        returncode = process.returncode

        if returncode != 0:
            error_msg = f"Command failed with exit code {returncode}"
            raise RuntimeError(error_msg + f"\nSTDERR: {''.join(stderr_lines)}")

        return {
            "stdout": "".join(stdout_lines),
            "stderr": "".join(stderr_lines),
            "returncode": returncode,
            "error": None
        }

    except Exception as e:
        print(f"❌ CI/CD step failed: {e}", file=sys.stderr)
        sys.exit(1)

class StarGitCIRunner:
    def __init__(self, repo_path: str, event_name: str = "push"):
        self.repo_path = Path(repo_path).resolve()
        self.event_name = event_name
        self.yml_path = self.repo_path / ".stargit" / "ci.yml"

    def load(self) -> dict:
        if not self.yml_path.exists():
            return {}
        try:
            with open(self.yml_path) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Failed to load .stargit/ci.yml: {e}")
            return {}

    def should_run(self, workflow: dict) -> bool:
        #print("workflow", workflow)
        on = workflow.get("on") or workflow.get(True) or []
        if isinstance(on, dict):
            on = on.keys()
        return self.event_name in on or "always" in on

    def run(self):
        workflow = self.load()
        if not workflow:
            print("No .stargit/ci.yml found — skipping CI")
            return

        if not self.should_run(workflow):
            print(f"Event '{self.event_name}' not in 'on:' — skipping")
            return

        print(f"StarGit CI/CD • Event: {self.event_name}")
        print(f"Repo: {self.repo_path.name}")
        print("-" * 50)

        jobs = workflow.get("jobs", {})
        for job_name, job in jobs.items():
            print(f"\nJob: {job_name}")
            steps = job.get("steps", [])

            for i, step in enumerate(steps, 1):
                name = step.get("name", f"Step {i}")
                script = step.get("run")

                if not script:
                    print(f"   • {name} (no run)")
                    continue

                print(f"   → {name}")
                print(f"     $ {script}")

                if False:
                    result = subprocess.run(
                        #script,  # run sh
                        ["bash", "-c", script],
                        shell=True,
                        cwd=str(self.repo_path),
                        capture_output=True,
                        text=True
                    )

                result = run_command(script)

                #print("result", result)
                #print("return code", result["returncode"])
                
                if result["stdout"]:
                    print(result["stdout"].rstrip())
                if result["stderr"]:
                    print(result["stderr"].rstrip(), file=sys.stderr)

                if result["returncode"] == 0:
                    print(f"   Success Step {i} succeeded")
                else:
                    print(f"   Failed Step {i} failed (exit {result["returncode"]})")
                    if job.get("continue-on-error"):
                        print("   Warning Continuing (continue-on-error)")
                    else:
                        print("   Failed Pipeline failed")
                        sys.exit(1)

        print("\nCI/CD completed successfully!")
        sys.exit(0)

# === USAGE FROM YOUR SYSTEM ===
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python stargit_ci.py <repo_path> [event]")
        sys.exit(1)
    repo = sys.argv[1]
    event = sys.argv[2] if len(sys.argv) > 2 else "push"
    StarGitCIRunner(repo, event).run()
