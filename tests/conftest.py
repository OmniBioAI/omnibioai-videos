"""Shared test-environment handling for optional Docker integration tests."""

import shutil
import subprocess

import pytest


def pytest_runtest_setup(item):
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
