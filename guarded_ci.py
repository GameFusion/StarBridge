"""StarBridge guarded CI adapter. Never switches branches or executes an editor buffer."""
import hashlib
import os
from pathlib import Path
import signal
import selectors
import subprocess
import tempfile
import time
import yaml
from pr_git import git, GitReviewError
from commit_read import exact_sha


def execute(path, params):
    path = Path(path).resolve()
    sha = exact_sha(params.get('expected_sha'))
    if git(path, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.decode().strip() != sha: raise GitReviewError('Checkout moved; CI cancelled.')
    if git(path, 'status', '--porcelain', '--untracked-files=no').stdout.strip(): raise GitReviewError('Tracked changes present; CI cancelled.')
    blob = git(path, 'show', sha+':.stargit/ci.yml').stdout
    file = path/'.stargit/ci.yml'
    if file.is_symlink() or file.parent.is_symlink() or not file.is_file() or file.resolve().parent != (path/'.stargit').resolve(): raise GitReviewError('Invalid CI configuration path.')
    if len(blob)>200_000 or hashlib.sha256(blob).hexdigest() != params.get('configuration_sha256') or file.read_bytes() != blob: raise GitReviewError('CI configuration does not match the requested commit/hash.')
    config=yaml.safe_load(blob)
    if not isinstance(config, dict) or not isinstance(config.get('jobs'), dict) or not 1<=len(config['jobs'])<=20: raise GitReviewError('Invalid CI jobs.')
    events=config.get('on', config.get(True, []))
    if isinstance(events, dict): events=list(events)
    if not isinstance(events, list) or not any(e in events for e in ('manual','always')): raise GitReviewError('Committed configuration must enable manual or always.')
    scripts=[]
    for name, job in config['jobs'].items():
        if not isinstance(job, dict) or set(job)-{'steps','name','continue-on-error'}: raise GitReviewError('Guarded CI supports steps, name and continue-on-error only.')
        steps=job.get('steps', [])
        if not isinstance(steps, list) or len(steps)>50: raise GitReviewError('Too many CI steps.')
        for step in steps:
            if not isinstance(step, dict) or set(step)-{'name','run'} or not isinstance(step.get('run'), str) or len(step['run'])>10_000: raise GitReviewError('Guarded CI supports named shell steps only.')
            scripts.append((step.get('name', str(name)), step['run'], job.get('continue-on-error') is True))
    started=time.monotonic(); code=0
    with tempfile.TemporaryFile() as log:
        for name, script, continue_on_error in scripts:
            remaining=600-(time.monotonic()-started)
            if remaining<=0: code=124; break
            log.write((str(name)+'\n').encode())
            process=subprocess.Popen(['/bin/bash','-e','-c',script],cwd=path,env={'PATH':os.defpath,'LANG':'C.UTF-8'},stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+min(300,remaining)
            with selectors.DefaultSelector() as reader:
                reader.register(process.stdout,selectors.EVENT_READ)
                while reader.get_map():
                    if time.monotonic()>deadline or log.tell()>=2_000_000:
                        code=124 if time.monotonic()>deadline else 125
                        try: os.killpg(process.pid,signal.SIGKILL)
                        except ProcessLookupError: pass
                        break
                    for key, _ in reader.select(timeout=0.1):
                        chunk=os.read(key.fileobj.fileno(),min(65_536,2_000_000-log.tell()))
                        if chunk: log.write(chunk)
                        else: reader.unregister(key.fileobj)
            process.stdout.close(); process.wait()
            if code: break
            if process.returncode and not continue_on_error: code=process.returncode; break
            if log.tell()>2_000_000: code=125; break
        log.seek(0); output=log.read(200_000).decode('utf-8','replace')
    return dict(exit_code=code, stdout=output, stderr='', commit_sha=sha, configuration_sha256=params['configuration_sha256'],
                duration_seconds=round(time.monotonic()-started,3), status='success' if code==0 else 'failed')
