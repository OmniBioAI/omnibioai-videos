"""Superseded duplicate of test_generated_infra.py's Docker infrastructure tests (kept out of
coverage per pyproject.toml): build and run the packaged nginx image, then exercise its root
route, manifest headers, video headers, SPA fallback, and health endpoint.

Developer: Manish Kumar <manish@omnibioai.org>
"""

import subprocess
import time
import pytest
import requests

pytestmark = pytest.mark.docker

IMAGE_NAME = "omnibioai-videos-test"
CONTAINER_NAME = "omnibioai-videos-test-container"
HOST_PORT = 18086
CONTAINER_PORT = 8086
BASE_URL = f"http://localhost:{HOST_PORT}"

@pytest.fixture(scope="module", autouse=True)
def docker_container():
    """Build the image and start the container for the module, removing any leftover container from
    a prior run first and cleaning up afterwards."""
    # A failed/interrupted previous run can leave the fixed-name container
    # behind. Remove only that test-owned container before starting.
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Build the image
    print("\nBuilding Docker image...")
    subprocess.run(["docker", "build", "-t", IMAGE_NAME, "."], check=True)

    # Run the container
    print("Starting Docker container...")
    subprocess.run([
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "-p", f"{HOST_PORT}:{CONTAINER_PORT}",
        IMAGE_NAME
    ], check=True)

    # Wait for the service to be ready
    max_retries = 10
    for i in range(max_retries):
        try:
            response = requests.get(f"{BASE_URL}/health")
            if response.status_code == 200:
                print("Service is ready!")
                break
        except requests.exceptions.ConnectionError:
            pass
        print(f"Waiting for service... ({i+1}/{max_retries})")
        time.sleep(1)
    else:
        # Final check if loop finishes without break
        try:
            requests.get(f"{BASE_URL}/health")
        except Exception as e:
            # Cleanup before failing
            subprocess.run(["docker", "stop", CONTAINER_NAME])
            subprocess.run(["docker", "rm", CONTAINER_NAME])
            pytest.fail(f"Service failed to start: {e}")

    yield

    # Cleanup, including after a test failure.
    print("\nStopping and removing Docker container...")
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def test_root_returns_index():
    """Serve the SPA index as HTML with a title from the container's root route."""
    response = requests.get(BASE_URL)
    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]
    assert "<title>" in response.text  # Assuming index.html has a title

def test_manifest_headers():
    """Serve videos.json with no-cache and a wildcard CORS header, exercised via this module's own
    container fixture."""
    response = requests.get(f"{BASE_URL}/videos.json")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Access-Control-Allow-Origin"] == "*"

def test_video_headers():
    """Advertise byte-range support, a day-long cache, and a wildcard CORS header on a served video
    file, when the file is present."""
    # Test with a known video file from manifest or a mock one
    # content/my_video.mov exists according to the file listing
    response = requests.get(f"{BASE_URL}/my_video.mov")
    if response.status_code == 200:
        assert response.headers["Accept-Ranges"] == "bytes"
        assert "max-age=86400" in response.headers["Cache-Control"]
        assert response.headers["Access-Control-Allow-Origin"] == "*"
    else:
        pytest.skip("my_video.mov not found in container, skipping header test")

def test_spa_fallback():
    """Fall back to the SPA index as HTML for an unrecognized path, exercised via this module's own
    container fixture."""
    # Random paths should return index.html
    response = requests.get(f"{BASE_URL}/some/random/path")
    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]
    # Check if it's actually the index.html content
    assert "<title>" in response.text

def test_health_endpoint():
    """Report status ok from the container's health endpoint."""
    response = requests.get(f"{BASE_URL}/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
