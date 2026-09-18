"""Shared test-environment handling for optional Docker integration tests.

Developer: Manish Kumar <manish@omnibioai.org>
"""

import shutil
import subprocess

import pytest


def pytest_runtest_setup(item):
    """Skip a Docker-marked test when the docker executable is missing or the Docker daemon is
    unreachable, leaving every other test untouched."""
    if "docker" not in item.keywords:
        return
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker integration tests require the docker executable")
    result = subprocess.run(
        [docker, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    )
    if result.returncode != 0:
        pytest.skip("Docker integration tests require access to the Docker daemon")
