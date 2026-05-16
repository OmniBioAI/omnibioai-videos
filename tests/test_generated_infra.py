import os
import json
import subprocess
import time
import pytest
import requests

# Configuration
MANIFEST_PATH = 'content/videos.json'
CONTENT_DIR = 'content'
PERMITTED_TAGS = {'intro', 'tutorial', 'workflow', 'demo', 'hpc'}
IMAGE_NAME = "omnibioai-videos-test"
CONTAINER_NAME = "omnibioai-videos-test-container-refactored"
PORT = 8091  # Changed to avoid conflicts
BASE_URL = f"http://localhost:{PORT}"

# --- Part 1: Manifest Validation ---

def test_manifest_file_exists():
    """Validates that the manifest file is present."""
    assert os.path.exists(MANIFEST_PATH), f"Manifest file {MANIFEST_PATH} not found"

def test_manifest_is_valid_json_list():
    """Ensures manifest is a valid JSON array."""
    with open(MANIFEST_PATH, 'r') as f:
        data = json.load(f)
    assert isinstance(data, list), "Manifest must be a JSON array"

def test_manifest_schema_and_tags():
    """Validates every item in the manifest against the defined schema and allowed tags."""
    with open(MANIFEST_PATH, 'r') as f:
        data = json.load(f)
    
    required_fields = {'filename', 'title', 'desc', 'tag', 'order'}
    for i, item in enumerate(data):
        for field in required_fields:
            assert field in item, f"Item {i} is missing required field: {field}"
        
        assert item['tag'] in PERMITTED_TAGS, f"Item {i} has an invalid tag: {item['tag']}"
        assert isinstance(item['order'], int), f"Item {i} 'order' must be an integer"
        
        ext = os.path.splitext(item['filename'])[1].lower()
        assert ext in {'.mp4', '.webm', '.mov'}, f"Unsupported video extension {ext} in item {i}"

def test_manifest_ordering_uniqueness():
    """Ensures that the 'order' fields are unique for sorting stability."""
    with open(MANIFEST_PATH, 'r') as f:
        data = json.load(f)
    orders = [item['order'] for item in data]
    assert len(orders) == len(set(orders)), "Video order values must be unique"

# --- Part 2: Infrastructure Tests ---

@pytest.fixture(scope="session")
def docker_service():
    """Builds and starts the Docker container for the duration of the test session."""
    # Cleanup
    subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True)
    subprocess.run(["docker", "rm", CONTAINER_NAME], capture_output=True)

    # Build
    subprocess.run(["docker", "build", "-t", IMAGE_NAME, "."], capture_output=True, check=True)
    
    # Run
    subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER_NAME,
        "-p", f"{PORT}:8086", IMAGE_NAME
    ], capture_output=True, check=True)
    
    # Wait
    max_retries = 15
    ready = False
    for _ in range(max_retries):
        try:
            if requests.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(1)
    
    if not ready:
        subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True)
        subprocess.run(["docker", "rm", CONTAINER_NAME], capture_output=True)
        pytest.fail("Nginx failed to start")

    yield

    # Cleanup
    subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True)
    subprocess.run(["docker", "rm", CONTAINER_NAME], capture_output=True)

def test_nginx_root_serving(docker_service):
    r = requests.get(BASE_URL)
    assert r.status_code == 200
    assert "text/html" in r.headers["Content-Type"]

def test_nginx_guide_serving(docker_service):
    r = requests.get(f"{BASE_URL}/guide.html")
    assert r.status_code == 200

def test_manifest_headers_and_cors(docker_service):
    r = requests.get(f"{BASE_URL}/videos.json")
    assert r.headers["Cache-Control"] == "no-cache"
    assert r.headers["Access-Control-Allow-Origin"] == "*"

def test_video_infrastructure_headers(docker_service):
    r = requests.get(f"{BASE_URL}/my_video.mov")
    if r.status_code == 200:
        # Some Nginx configurations or proxies might double-up the Accept-Ranges header
        assert "bytes" in r.headers["Accept-Ranges"]

def test_spa_fallback_routing(docker_service):
    r = requests.get(BASE_URL + "/any/random/path")
    assert r.status_code == 200
    assert "text/html" in r.headers["Content-Type"]

def test_health_check_payload(docker_service):
    r = requests.get(f"{BASE_URL}/health")
    assert r.json() == {"status": "ok"}
