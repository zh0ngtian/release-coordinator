"""Verify remote commit inputs without holding a registry transaction."""
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager

from .store import Conflict


@contextmanager
def _fetch(config, sha):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Ready requires a full commit SHA')
    # A fetch into the project's existing object database can succeed for an
    # unpushed local commit without proving that the remote owns the object.
    with tempfile.TemporaryDirectory(prefix='release-commit-verification-') as repo:
        try:
            subprocess.run(['git', 'init', '--bare', repo], check=True,
                capture_output=True, text=True, timeout=10)
            result = subprocess.run(['git', '-c', 'http.version=HTTP/1.1', '-C', repo,
                'fetch', '--depth=1', '--filter=blob:none', '--no-tags', '--no-write-fetch-head', config['remote'], sha],
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, capture_output=True, text=True, timeout=120)
            yield repo, result
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            raise ValueError('Remote commit verification failed; task remains unready') from error


def verify_remote_commit(config, sha):
    with _fetch(config, sha) as (repo, result):
        if result.returncode:
            raise ValueError('Cannot fetch the exact commit from the configured remote; task remains unready')
        kind = subprocess.run(['git', '-C', repo, 'cat-file', '-t', sha],
            capture_output=True, text=True, timeout=10)
        if kind.returncode or kind.stdout.strip() != 'commit':
            raise ValueError('Ready SHA must identify a commit object')


def verify_missing_commit(config, sha):
    with _fetch(config, sha) as (_, result):
        if not result.returncode:
            raise Conflict('The original remote commit exists; use ordinary linked recovery')
        if 'not our ref ' + sha not in result.stderr:
            raise ValueError('Cannot confirm that the original remote commit is unavailable')
