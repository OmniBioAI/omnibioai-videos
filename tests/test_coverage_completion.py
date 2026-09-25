"""Unit coverage for the non-Docker branches of the integration tests."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_test_module(filename, name):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generated_infra = _load_test_module("test_generated_infra.py", "generated_infra")
manifest_tests = _load_test_module("test_manifest.py", "manifest_tests")
static_tests = _load_test_module("test_static_contracts.py", "static_tests")
conftest = _load_test_module("conftest.py", "video_conftest")


class FakeResponse:
    status_code = 200
    text = "<title>OmniBioAI Videos</title>"
    headers = {
        "Content-Type": "text/html",
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
    }

    def json(self):
        return {"status": "ok"}


def test_generated_http_contracts_without_docker(monkeypatch):
    monkeypatch.setattr(generated_infra.requests, "get", lambda *args, **kwargs: FakeResponse())
    service = SimpleNamespace()
    generated_infra.test_nginx_root_serving(service)
    generated_infra.test_nginx_guide_serving(service)
    generated_infra.test_manifest_headers_and_cors(service)
    generated_infra.test_video_infrastructure_headers(service)
    generated_infra.test_spa_fallback_routing(service)
    generated_infra.test_health_check_payload(service)


def test_generated_manifest_contracts_without_docker():
    generated_infra.test_manifest_file_exists()
    generated_infra.test_manifest_is_valid_json_list()
    generated_infra.test_manifest_schema_and_tags()
    generated_infra.test_manifest_ordering_uniqueness()


def test_docker_service_fixture_success_path(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(generated_infra.subprocess, "run", fake_run)
    monkeypatch.setattr(generated_infra.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(generated_infra.time, "sleep", lambda seconds: None)

    fixture = generated_infra.docker_service.__wrapped__()
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)

    assert commands[0][0:2] == ["docker", "stop"]
    assert ["docker", "build", "-t", generated_infra.IMAGE_NAME, "."] in commands


def test_docker_service_fixture_failure_path(monkeypatch):
    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(generated_infra.subprocess, "run", fake_run)
    monkeypatch.setattr(
        generated_infra.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    monkeypatch.setattr(generated_infra.time, "sleep", lambda seconds: None)

    fixture = generated_infra.docker_service.__wrapped__()
    with pytest.raises(pytest.fail.Exception, match="Nginx failed to start"):
        next(fixture)


def test_manifest_invalid_json_is_reported(monkeypatch, tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    (content / "videos.json").write_text("{invalid", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(pytest.fail.Exception, match="not valid JSON"):
        manifest_tests.test_manifest_is_valid_json()


def test_docker_skip_and_known_routing_failure_branches(monkeypatch):
    item = SimpleNamespace(keywords={"docker"})
    monkeypatch.setattr(conftest.shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception, match="docker executable"):
        conftest.pytest_runtest_setup(item)

    monkeypatch.setattr(conftest.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        conftest.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(pytest.skip.Exception, match="Docker daemon"):
        conftest.pytest_runtest_setup(item)

    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda self, encoding: "fetch(`${API}/videos.json`) fetch(`${API}/videos/`)",
        )
        with pytest.raises(AssertionError):
            static_tests.test_index_uses_the_same_video_urls_as_the_nginx_configuration()
