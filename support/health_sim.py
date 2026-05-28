#!/usr/bin/env python3
"""
Simulate a mic-tower health check uplink.

Usage:
    python simulate_health.py <dev_eui> ok       # Sends a healthy mic-check (class 1022)
    python simulate_health.py <dev_eui> error     # Sends a mic error (class 1023)

Options:
    --url   Gateway base URL (default: http://localhost:5000)

Examples:
    python simulate_health.py 0409221920260001 ok
    python simulate_health.py 0409221920260001 error --url http://192.168.1.50:5000
"""

import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Must match server.py and map.html CONFIG
HEALTH_OK_CLASS_ID = 1022
HEALTH_ERROR_CLASS_ID = 1023

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    dev_eui = sys.argv[1]
    status = sys.argv[2].lower()
    base_url = "http://localhost:80"

    # Parse optional --url flag
    if "--url" in sys.argv:
        idx = sys.argv.index("--url")
        if idx + 1 < len(sys.argv):
            base_url = sys.argv[idx + 1]

    if status == "ok":
        class_id = HEALTH_OK_CLASS_ID
    elif status == "error":
        class_id = HEALTH_ERROR_CLASS_ID
    else:
        print(f"Unknown status '{status}'. Use 'ok' or 'error'.")
        sys.exit(1)

    # Build a minimal ChirpStack-style uplink payload
    now = datetime.now(timezone.utc)
    seconds_since_midnight = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6

    payload = {
        "deviceInfo": {
            "devEui": dev_eui
        },
        "time": now.isoformat(),
        "object": {
            "detections": [
                {
                    "class_id": class_id,
                    "azimuth": 0.0,
                    "node_time": round(seconds_since_midnight, 1)
                }
            ]
        },
        "rxInfo": []
    }

    url = f"{base_url.rstrip('/')}/uplink?event=up"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    print(f"Sending health '{status}' (class_id={class_id}) for node {dev_eui}")
    print(f"  → POST {url}")

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            print(f"  ✅ {resp.status}: {json.dumps(body)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  ❌ {e.code}: {body}")
    except urllib.error.URLError as e:
        print(f"  ❌ Connection failed: {e.reason}")

if __name__ == "__main__":
    main()
