import json
import os
import pytest

MANIFEST_PATH = 'content/videos.json'
CONTENT_DIR = 'content'
PERMITTED_TAGS = {'intro', 'tutorial', 'workflow', 'demo', 'hpc'}

def test_manifest_exists():
    assert os.path.exists(MANIFEST_PATH), f"{MANIFEST_PATH} does not exist"

def test_manifest_is_valid_json():
    with open(MANIFEST_PATH, 'r') as f:
        try:
            data = json.load(f)
            assert isinstance(data, list), "Manifest should be a list"
        except json.JSONDecodeError as e:
            pytest.fail(f"Manifest is not valid JSON: {e}")

def test_manifest_is_valid_json_reports_decode_errors(tmp_path):
    global MANIFEST_PATH
    bad_manifest = tmp_path / "bad.json"
    bad_manifest.write_text("{not valid json")
    original, MANIFEST_PATH = MANIFEST_PATH, str(bad_manifest)
    try:
        with pytest.raises(pytest.fail.Exception, match="not valid JSON"):
            test_manifest_is_valid_json()
    finally:
        MANIFEST_PATH = original

def test_manifest_schema():
    with open(MANIFEST_PATH, 'r') as f:
        data = json.load(f)
    
    required_fields = {'filename', 'title', 'desc', 'tag', 'order'}
    
    for i, item in enumerate(data):
        # Check required fields
        for field in required_fields:
            assert field in item, f"Item {i} is missing required field: {field}"
        
        # Validate tag
        assert item['tag'] in PERMITTED_TAGS, f"Item {i} has invalid tag: {item['tag']}"
        
        # Validate order
        assert isinstance(item['order'], int), f"Item {i} order should be an integer"
        
        # Validate filename exists in content directory
        video_path = os.path.join(CONTENT_DIR, item['filename'])
        assert os.path.exists(video_path), f"Video file {item['filename']} not found in {CONTENT_DIR}"

def test_manifest_ordering():
    with open(MANIFEST_PATH, 'r') as f:
        data = json.load(f)
    
    orders = [item['order'] for item in data]
    # Check if orders are unique (optional, but usually good)
    assert len(orders) == len(set(orders)), "Video orders should be unique"
