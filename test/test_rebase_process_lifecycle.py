"""Real CPU processes exercise detached test ownership and zombie locks."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from infermatrix_copilot.rebase_engine.processes import cleanup_run_processes
from infermatrix_copilot.testing.gpu_lock import GpuLock
from infermatrix_copilot.testing.process_tree import _alive, kill_tree

pytestmark = pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='requires procfs')


def test_zombie_owner_does_not_hold_gpu_mutex(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            if state == 'Z':
                break
            time.sleep(0.01)
        else:
            pytest.fail('child did not become an unreaped zombie')
        # kill(pid, 0) still succeeds: this was the old lock's liveness test.
        os.kill(child.pid, 0)
        tmp_path.joinpath('lock').write_text(str(child.pid))
        with GpuLock(tmp_path, poll_sec=0.01, timeout_sec=0.2):
            assert tmp_path.joinpath('lock').read_text() == str(os.getpid())
        assert not _alive(child.pid)
    finally:
        child.wait(timeout=5)


def test_cleanup_reaches_detached_orphan_and_preserves_other_runs(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    env = dict(os.environ, IMX_RUN_ID='fixture-run', IMX_LOG_DIR=str(run))
    launcher = subprocess.Popen([
        sys.executable, '-c',
        'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"], '
        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); '
        'print(p.pid,flush=True)',
    ], env=env, stdout=subprocess.PIPE, text=True)
    orphan = int(launcher.stdout.readline())
    launcher.wait(timeout=5)
    others = [subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'],
                              env=dict(env, **different)) for different in
              [{'IMX_RUN_ID': 'another-run'}, {'IMX_LOG_DIR': str(tmp_path / 'other')}]]
    try:
        assert _alive(orphan)
        assert cleanup_run_processes(run, 'fixture-run') == []
        assert not _alive(orphan)
        assert all(p.poll() is None for p in others)
    finally:
        kill_tree([orphan, *(p.pid for p in others)], term_grace=0.1, kill_grace=0.1)
        for p in others:
            p.wait(timeout=5)
        launcher.stdout.close()
