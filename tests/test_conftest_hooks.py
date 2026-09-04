"""Direct unit coverage for conftest.py's Docker-availability skip hook.

pytest_runtest_setup only ever runs (and its skip branches only ever
trigger) inside a real pytest session collecting a real test item, so the
two skip paths -- docker not installed, docker daemon unreachable -- are
exercised here by calling the hook directly against a minimal fake item,
rather than by actually uninstalling Docker.
"""

import shutil
import subprocess

import pytest

import conftest as _conftest


class _FakeItem:
    def __init__(self, keywords):
        self.keywords = keywords


def test_pytest_runtest_setup_ignores_non_docker_items():
    item = _FakeItem(keywords={})
    _conftest.pytest_runtest_setup(item)  # must not raise or skip


def test_pytest_runtest_setup_skips_when_docker_executable_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    item = _FakeItem(keywords={"docker": True})
    with pytest.raises(pytest.skip.Exception, match="docker executable"):
        _conftest.pytest_runtest_setup(item)


def test_pytest_runtest_setup_skips_when_docker_daemon_unreachable(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode=1)
    )
    item = _FakeItem(keywords={"docker": True})
    with pytest.raises(pytest.skip.Exception, match="Docker daemon"):
        _conftest.pytest_runtest_setup(item)
