"""Reap run-owned background tests, including reparented setsid chains."""
from __future__ import annotations

import os
from pathlib import Path

from ..testing.process_tree import _is_ancestor_of_self, _start_time, kill_tree


def cleanup_run_processes(run_dir: Path, run_id: str) -> list[int]:
    """Only same-user processes carrying BOTH exact run markers are owned.

    Descendant snapshots cannot find a shell that daemonized between samples.
    The inherited markers survive that reparenting. Record process birth times
    before killing so a reused PID cannot turn cleanup into a bystander kill.
    """
    if not run_id:
        return []
    root = Path(run_dir).resolve()
    identity: dict[int, int] = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            born = _start_time(pid)
            env = dict(item.split(b'=', 1) for item in
                       (entry / 'environ').read_bytes().split(b'\0') if b'=' in item)
            if env.get(b'IMX_RUN_ID') != os.fsencode(run_id):
                continue
            log_dir = env.get(b'IMX_LOG_DIR')
            if not log_dir or Path(os.fsdecode(log_dir)).resolve() != root:
                continue
            if born is not None and not _is_ancestor_of_self(pid):
                identity[pid] = born
        except (OSError, ValueError):
            continue
    return kill_tree(sorted(identity), identity=identity) if identity else []
