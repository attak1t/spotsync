#!/usr/bin/env python3
"""
Smoke test for SpotSync pipeline.

This script submits a single Spotify track URL to the API and polls until the job completes.
"""

import os
import sys
import time
import requests
import json
from typing import Optional

# Configuration
BASE_URL = "http://localhost:8080"
USERNAME = "admin"
PASSWORD = "changeme"  # Default password

def get_auth_token() -> str:
    """Get Basic Auth token."""
    import base64
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    return token

def test_health() -> bool:
    """Test health endpoint."""
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

def submit_job(track_url: str) -> Optional[str]:
    """Submit a job and return job ID."""
    token = get_auth_token()
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json"
    }

    data = {"url": track_url}

    try:
        response = requests.post(f"{BASE_URL}/api/jobs", headers=headers, json=data, timeout=10)
        response.raise_for_status()
        job = response.json()
        print(f"Job submitted: {job['id']}")
        print(f"  URL: {job['spotify_url']}")
        print(f"  Status: {job['status']}")
        print(f"  Tracks: {job['track_count']}")
        return job['id']
    except Exception as e:
        print(f"Failed to submit job: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        return None

def get_job_status(job_id: str) -> Optional[dict]:
    """Get job status and details."""
    token = get_auth_token()
    headers = {"Authorization": f"Basic {token}"}

    try:
        response = requests.get(f"{BASE_URL}/api/jobs/{job_id}", headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Failed to get job status: {e}")
        return None

def poll_job(job_id: str, max_wait: int = 600, poll_interval: int = 5) -> bool:
    """Poll job until completion or timeout."""
    print(f"\nPolling job {job_id}...")
    print("-" * 50)

    start_time = time.time()
    last_status = None

    while time.time() - start_time < max_wait:
        job = get_job_status(job_id)
        if not job:
            print("Failed to get job status")
            return False

        # Print status if changed
        if job['status'] != last_status:
            print(f"[{time.strftime('%H:%M:%S')}] Status: {job['status']}")
            last_status = job['status']

            # Count tracks by status
            if 'tracks' in job:
                status_counts = {}
                for track in job['tracks']:
                    status_counts[track['status']] = status_counts.get(track['status'], 0) + 1
                print(f"  Tracks: {status_counts}")

        # Check if job is done
        if job['status'] in ['done', 'failed']:
            print(f"\nJob {job['status']}!")

            # Print final track status
            if 'tracks' in job:
                print("\nFinal track status:")
                for track in job['tracks']:
                    print(f"  - {track['title']} ({track['artist']}): {track['status']}")
                    if track['error_message']:
                        print(f"    Error: {track['error_message']}")

            return job['status'] == 'done'

        time.sleep(poll_interval)

    print(f"\nTimeout after {max_wait} seconds")
    return False

def main():
    """Main test function."""
    print("SpotSync Smoke Test")
    print("=" * 50)

    # Check if URL provided as argument
    if len(sys.argv) > 1:
        track_url = sys.argv[1]
    else:
        # Default test track
        track_url = "https://open.spotify.com/track/5ghIJDpPoe3CfHMGu71E6T"  # Test track

    print(f"Testing with URL: {track_url}")

    # Check health
    print("\n1. Checking API health...")
    if not test_health():
        print("ERROR: API is not reachable. Make sure SpotSync is running.")
        print(f"  URL: {BASE_URL}")
        sys.exit(1)
    print("✓ API is reachable")

    # Submit job
    print("\n2. Submitting job...")
    job_id = submit_job(track_url)
    if not job_id:
        print("ERROR: Failed to submit job")
        sys.exit(1)

    # Poll for completion
    print("\n3. Waiting for job to complete...")
    success = poll_job(job_id, max_wait=300)  # 5 minute timeout

    if success:
        print("\n✓ Smoke test PASSED")
        sys.exit(0)
    else:
        print("\n✗ Smoke test FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()