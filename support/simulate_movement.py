#!/usr/bin/env python3
"""
Realistic movement simulator for the DIVS mic-tower system.

Simulates a target (e.g. aircraft, vehicle) moving in a straight line through
the detection area of two towers.  Models the real LoRaWAN uplink behaviour:

  - Detections are batched: 10 per uplink, one every 0.5s (= 5s window).
  - Each tower sends its batch independently with a random timing offset.
  - Each tower may classify the sound as a different class_id.
  - Azimuth has configurable noise (jitter).
  - A tower only detects the target while it is within range.

Usage:
    python simulate_movement.py                          # auto-pick 2 towers
    python simulate_movement.py --towers EUI1 EUI2       # specific towers
    python simulate_movement.py --speed 120 --heading 45 # override defaults
    python simulate_movement.py --url http://host:port   # custom gateway URL
    python simulate_movement.py --batches 20             # run for 20 batches

All parameters are optional and have sensible random defaults.
"""

import sys
import json
import math
import time
import random
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
# Tweak these in one place if the firmware / gateway behaviour changes.

BASE_URL            = "http://localhost:80"
BATCH_SIZE          = 10        # detections per uplink message
BATCH_INTERVAL_S    = 5.0       # seconds between batches (real-time pacing)
AZIMUTH_RESOLUTION  = 0.5       # seconds between azimuth samples inside a batch
AZIMUTH_NOISE_DEG   = 2.0       # ±degrees of random jitter on each azimuth
TOWER_TIMING_JITTER = 1.5       # max seconds offset between towers' batch sends
DETECTION_PROB      = 0.92      # probability a tower detects in any 0.5s slot
NUM_BATCHES         = 12        # how many 5-second batches to simulate (= 60s)

# Speed range (m/s) for random target — roughly 100–400 km/h
SPEED_MIN = 28
SPEED_MAX = 111

# Realistic class_id pools that each tower may independently pick from.
# Using aircraft / engine related classes so triangulation colours make sense.
CLASS_POOLS = [
    [329, 330, 334],            # Aircraft, Aircraft engine, Fixed-wing
    [331, 337, 342, 343],       # Jet engine, Engine, Medium engine, Heavy engine
    [332, 333],                 # Propeller, Helicopter
]

EARTH_RADIUS = 6378137.0  # metres


# ─── GEOMETRY HELPERS ─────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Distance in metres between two lat/lon points."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return EARTH_RADIUS * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Bearing in degrees (0 = North, 90 = East) from point 1 to point 2."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δλ = math.radians(lon2 - lon1)
    x = math.sin(Δλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def destination_point(lat, lon, bearing_deg_, distance_m):
    """Move from (lat, lon) along bearing by distance. Returns (lat, lon)."""
    φ1 = math.radians(lat)
    λ1 = math.radians(lon)
    θ = math.radians(bearing_deg_)
    δ = distance_m / EARTH_RADIUS
    φ2 = math.asin(math.sin(φ1) * math.cos(δ) + math.cos(φ1) * math.sin(δ) * math.cos(θ))
    λ2 = λ1 + math.atan2(math.sin(θ) * math.sin(δ) * math.cos(φ1),
                          math.cos(δ) - math.sin(φ1) * math.sin(φ2))
    return math.degrees(φ2), math.degrees(λ2)


# ─── NETWORK HELPERS ──────────────────────────────────────────────────────────

def fetch_json(url):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def post_uplink(base_url, dev_eui, detections, gateway_id=None):
    """Send a ChirpStack-style uplink to the gateway."""
    now = datetime.now(timezone.utc)
    payload = {
        "deviceInfo": {"devEui": dev_eui},
        "time": now.isoformat(),
        "object": {"detections": detections},
        "rxInfo": [{"gatewayId": gateway_id}] if gateway_id else [],
    }
    url = f"{base_url.rstrip('/')}/uplink?event=up"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return 0, str(e.reason)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    base_url = BASE_URL
    tower_euis = None
    speed = None
    heading = None
    num_batches = NUM_BATCHES

    # ── Parse CLI args ─────────────────────────────────────────────────────
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--url" and i + 1 < len(args):
            base_url = args[i + 1]; i += 2
        elif args[i] == "--towers" and i + 2 < len(args):
            tower_euis = [args[i + 1], args[i + 2]]; i += 3
        elif args[i] == "--speed" and i + 1 < len(args):
            speed = float(args[i + 1]); i += 2
        elif args[i] == "--heading" and i + 1 < len(args):
            heading = float(args[i + 1]); i += 2
        elif args[i] == "--batches" and i + 1 < len(args):
            num_batches = int(args[i + 1]); i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__); sys.exit(0)
        else:
            print(f"Unknown argument: {args[i]}"); print(__doc__); sys.exit(1)

    # ── Fetch towers from the API ──────────────────────────────────────────
    print(f"Fetching nodes from {base_url} …")
    nodes = fetch_json(f"{base_url.rstrip('/')}/nodes")
    if not isinstance(nodes, list) or len(nodes) < 2:
        print("Need at least 2 registered nodes. Aborting."); sys.exit(1)

    if tower_euis:
        towers = [n for n in nodes if n["dev_eui"] in tower_euis]
        if len(towers) < 2:
            print(f"Could not find both tower EUIs. Available: {[n['dev_eui'] for n in nodes]}")
            sys.exit(1)
    else:
        towers = random.sample(nodes, 2)

    t1, t2 = towers[0], towers[1]
    print(f"Tower A: {t1['name'] or t1['dev_eui']}  ({t1['latitude']:.6f}, {t1['longitude']:.6f})  range={t1['range']}m")
    print(f"Tower B: {t2['name'] or t2['dev_eui']}  ({t2['latitude']:.6f}, {t2['longitude']:.6f})  range={t2['range']}m")

    # ── Derive random target trajectory ────────────────────────────────────
    if speed is None:
        speed = random.uniform(SPEED_MIN, SPEED_MAX)
    if heading is None:
        heading = random.uniform(0, 360)

    # Place start position within range of at least one tower.
    # Pick a random point inside Tower A's range, offset by a random bearing.
    start_bearing = random.uniform(0, 360)
    start_dist = random.uniform(0, float(t1["range"]) * 0.85)
    start_lat, start_lon = destination_point(t1["latitude"], t1["longitude"],
                                             start_bearing, start_dist)

    total_time = num_batches * BATCH_INTERVAL_S
    total_dist = speed * total_time
    end_lat, end_lon = destination_point(start_lat, start_lon, heading, total_dist)

    print(f"\nTarget trajectory:")
    print(f"  Heading : {heading:.1f}°")
    print(f"  Speed   : {speed:.1f} m/s  ({speed * 3.6:.0f} km/h)")
    print(f"  Duration: {total_time:.0f}s  ({num_batches} batches × {BATCH_INTERVAL_S:.0f}s)")
    print(f"  Start   : ({start_lat:.6f}, {start_lon:.6f})")
    print(f"  End     : ({end_lat:.6f}, {end_lon:.6f})")

    # Assign each tower a random class_id pool for this run (simulates
    # independent ML classification — they may hear the same sound but
    # classify it differently).
    pool_a = random.choice(CLASS_POOLS)
    pool_b = random.choice(CLASS_POOLS)
    print(f"\n  Tower A class pool: {pool_a}")
    print(f"  Tower B class pool: {pool_b}")
    print()

    # ── Simulate batch-by-batch ────────────────────────────────────────────
    sim_start_time = time.time()

    for batch_idx in range(num_batches):
        batch_wall_start = time.time()
        batch_t0 = batch_idx * BATCH_INTERVAL_S  # simulation seconds

        # Build per-tower detection lists for this 5-second window
        dets_a, dets_b = [], []

        for slot in range(BATCH_SIZE):
            t = batch_t0 + slot * AZIMUTH_RESOLUTION  # simulation second of this sample
            # Current target position
            d = speed * t
            tgt_lat, tgt_lon = destination_point(start_lat, start_lon, heading, d)

            # UTC seconds since midnight for node_time (0.1s precision)
            utc_now = datetime.now(timezone.utc)
            base_secs = utc_now.hour * 3600 + utc_now.minute * 60 + utc_now.second
            node_time = round(base_secs + slot * AZIMUTH_RESOLUTION, 1)

            # Tower A
            dist_a = haversine(t1["latitude"], t1["longitude"], tgt_lat, tgt_lon)
            if dist_a <= float(t1["range"]) and random.random() < DETECTION_PROB:
                az_a = bearing_deg(t1["latitude"], t1["longitude"], tgt_lat, tgt_lon)
                az_a += random.gauss(0, AZIMUTH_NOISE_DEG)
                az_a = round(az_a % 360, 2)
                dets_a.append({
                    "class_id": random.choice(pool_a),
                    "azimuth": az_a,
                    "node_time": node_time,
                })

            # Tower B
            dist_b = haversine(t2["latitude"], t2["longitude"], tgt_lat, tgt_lon)
            if dist_b <= float(t2["range"]) and random.random() < DETECTION_PROB:
                az_b = bearing_deg(t2["latitude"], t2["longitude"], tgt_lat, tgt_lon)
                az_b += random.gauss(0, AZIMUTH_NOISE_DEG)
                az_b = round(az_b % 360, 2)
                dets_b.append({
                    "class_id": random.choice(pool_b),
                    "azimuth": az_b,
                    "node_time": node_time,
                })

        # Send the batches with a random timing offset between the two towers
        offset = random.uniform(0, TOWER_TIMING_JITTER)
        send_a_first = random.choice([True, False])

        def send_batch(tower, dets, label):
            if not dets:
                print(f"  {label}: (out of range — 0 detections)")
                return
            gw_id = tower.get("connected_gateway")
            status, body = post_uplink(base_url, tower["dev_eui"], dets, gw_id)
            print(f"  {label}: {len(dets)} dets → HTTP {status}")

        tag = f"Batch {batch_idx + 1}/{num_batches}"
        elapsed = batch_t0
        print(f"[{tag}]  t={elapsed:.0f}–{elapsed + BATCH_INTERVAL_S:.0f}s")

        if send_a_first:
            send_batch(t1, dets_a, f"Tower A ({t1['name'] or t1['dev_eui'][:8]})")
            if offset > 0:
                time.sleep(offset)
            send_batch(t2, dets_b, f"Tower B ({t2['name'] or t2['dev_eui'][:8]})")
        else:
            send_batch(t2, dets_b, f"Tower B ({t2['name'] or t2['dev_eui'][:8]})")
            if offset > 0:
                time.sleep(offset)
            send_batch(t1, dets_a, f"Tower A ({t1['name'] or t1['dev_eui'][:8]})")

        # Pace to real-time (minus the time we already spent)
        batch_elapsed = time.time() - batch_wall_start
        sleep_remaining = max(0, BATCH_INTERVAL_S - batch_elapsed)
        if batch_idx < num_batches - 1 and sleep_remaining > 0:
            time.sleep(sleep_remaining)

    print(f"\n✅ Simulation complete — {num_batches} batches sent over {time.time() - sim_start_time:.1f}s")


if __name__ == "__main__":
    main()
