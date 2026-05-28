#!/usr/bin/env python3
"""
Realistic continuous movement simulator for the DIVS mic-tower system.

Simulates targets (e.g. aircraft, vehicles) moving in straight lines through
the detection area of two towers.  Runs continuously — after each pass
finishes, new random values are picked and another target begins.

Models real LoRaWAN uplink behaviour:
  - Detections are batched: 10 per uplink, one every 0.5s (= 5s window).
  - Each tower sends its batch independently with a random timing offset.
  - Each tower may classify the sound as a different class_id.
  - Azimuth has configurable noise (jitter).
  - A tower only detects the target while it is within range.

Usage:
    python movement_sim.py                              # auto-pick 2 towers, loop forever
    python movement_sim.py --towers EUI1 EUI2           # lock to specific towers
    python movement_sim.py --url http://host:port       # custom gateway URL
    python movement_sim.py --batches 20                 # 20 batches per pass

Press Ctrl+C to stop.
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
BATCHES_PER_PASS    = 12        # batches per pass (= 60s at 5s intervals)

# Cooldown between passes (seconds) — random within this range
PASS_COOLDOWN_MIN   = 2.0
PASS_COOLDOWN_MAX   = 8.0
MAX_SILENT_BATCHES  = 2         # abort pass if BOTH towers see nothing for this many consecutive batches

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
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Bearing in degrees (0 = North, 90 = East) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def destination_point(lat, lon, bearing_deg_, distance_m):
    """Move from (lat, lon) along bearing by distance. Returns (lat, lon)."""
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    th = math.radians(bearing_deg_)
    d = distance_m / EARTH_RADIUS
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(th))
    l2 = l1 + math.atan2(math.sin(th) * math.sin(d) * math.cos(p1),
                          math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


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


# ─── SINGLE PASS ──────────────────────────────────────────────────────────────

def run_pass(pass_num, t1, t2, base_url, num_batches):
    """Simulate one linear target movement across two towers."""

    speed = random.uniform(SPEED_MIN, SPEED_MAX)

    # ── Build a trajectory that crosses through BOTH towers' ranges ──
    # Strategy: compute the midpoint between the two towers, then create
    # a path that sweeps roughly perpendicular to the tower-to-tower line
    # so it passes through the overlap zone.
    mid_lat = (t1["latitude"] + t2["latitude"]) / 2
    mid_lon = (t1["longitude"] + t2["longitude"]) / 2

    # Bearing from T1 to T2
    t1_to_t2_bearing = bearing_deg(t1["latitude"], t1["longitude"],
                                   t2["latitude"], t2["longitude"])

    # Pick a heading roughly perpendicular to the T1-T2 axis (+/-30 deg jitter)
    perp = t1_to_t2_bearing + 90 + random.uniform(-30, 30)
    heading = perp % 360

    # How far away from the midpoint to start (so the target enters, crosses,
    # and exits both ranges).  Use the larger tower range as reference.
    max_range = max(float(t1["range"]), float(t2["range"]))
    approach_dist = max_range * random.uniform(0.8, 1.3)

    # Also offset slightly along the T1-T2 axis so paths aren't always dead-centre
    lateral_offset = random.uniform(-max_range * 0.3, max_range * 0.3)
    offset_lat, offset_lon = destination_point(mid_lat, mid_lon,
                                               t1_to_t2_bearing, lateral_offset)

    # Start point: approach_dist metres BEHIND the midpoint (opposite of heading)
    start_lat, start_lon = destination_point(offset_lat, offset_lon,
                                             (heading + 180) % 360, approach_dist)

    total_time = num_batches * BATCH_INTERVAL_S
    total_dist = speed * total_time
    end_lat, end_lon = destination_point(start_lat, start_lon, heading, total_dist)

    # Each tower gets a random class pool for this pass
    pool_a = random.choice(CLASS_POOLS)
    pool_b = random.choice(CLASS_POOLS)

    print(f"\n{'='*70}")
    print(f"  PASS #{pass_num}")
    print(f"{'='*70}")
    print(f"  Heading : {heading:.1f} deg   Speed: {speed:.1f} m/s ({speed * 3.6:.0f} km/h)")
    print(f"  Duration: {total_time:.0f}s max ({num_batches} batches)")
    print(f"  Start   : ({start_lat:.6f}, {start_lon:.6f})")
    print(f"  End     : ({end_lat:.6f}, {end_lon:.6f})")
    print(f"  Classes : A={pool_a}  B={pool_b}")
    print()

    name_a = t1['name'] or t1['dev_eui'][:8]
    name_b = t2['name'] or t2['dev_eui'][:8]
    sim_start = time.time()
    consecutive_silent = 0       # batches where BOTH towers had 0 detections

    for batch_idx in range(num_batches):
        batch_wall_start = time.time()
        batch_t0 = batch_idx * BATCH_INTERVAL_S

        dets_a, dets_b = [], []

        for slot in range(BATCH_SIZE):
            t = batch_t0 + slot * AZIMUTH_RESOLUTION
            dist = speed * t
            tgt_lat, tgt_lon = destination_point(start_lat, start_lon, heading, dist)

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

        # Track consecutive silent batches (both towers empty)
        if len(dets_a) == 0 and len(dets_b) == 0:
            consecutive_silent += 1
        else:
            consecutive_silent = 0

        # Send batches with random timing offset between towers
        offset = random.uniform(0, TOWER_TIMING_JITTER)
        send_a_first = random.choice([True, False])

        def send_batch(tower, dets, label):
            if not dets:
                print(f"    {label}: (out of range)")
                return
            gw_id = tower.get("connected_gateway")
            status, _ = post_uplink(base_url, tower["dev_eui"], dets, gw_id)
            print(f"    {label}: {len(dets)} dets -> {status}")

        print(f"  [Batch {batch_idx + 1}/{num_batches}]  t={batch_t0:.0f}-{batch_t0 + BATCH_INTERVAL_S:.0f}s")

        if send_a_first:
            send_batch(t1, dets_a, f"A ({name_a})")
            if offset > 0:
                time.sleep(offset)
            send_batch(t2, dets_b, f"B ({name_b})")
        else:
            send_batch(t2, dets_b, f"B ({name_b})")
            if offset > 0:
                time.sleep(offset)
            send_batch(t1, dets_a, f"A ({name_a})")

        # Abort early if out of range for too long
        if consecutive_silent >= MAX_SILENT_BATCHES:
            print(f"\n  Pass #{pass_num} aborted - both towers silent for {consecutive_silent} consecutive batches")
            return

        # Pace to real-time
        batch_elapsed = time.time() - batch_wall_start
        sleep_remaining = max(0, BATCH_INTERVAL_S - batch_elapsed)
        if batch_idx < num_batches - 1 and sleep_remaining > 0:
            time.sleep(sleep_remaining)

    elapsed = time.time() - sim_start
    print(f"\n  Pass #{pass_num} done - {num_batches} batches in {elapsed:.1f}s")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    base_url = BASE_URL
    tower_euis = None
    num_batches = BATCHES_PER_PASS

    # ── Parse CLI args ─────────────────────────────────────────────────────
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--url" and i + 1 < len(args):
            base_url = args[i + 1]; i += 2
        elif args[i] == "--towers" and i + 2 < len(args):
            tower_euis = [args[i + 1], args[i + 2]]; i += 3
        elif args[i] == "--batches" and i + 1 < len(args):
            num_batches = int(args[i + 1]); i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__); sys.exit(0)
        else:
            print(f"Unknown argument: {args[i]}"); print(__doc__); sys.exit(1)

    # ── Fetch towers from the API ──────────────────────────────────────────
    print(f"Fetching nodes from {base_url} ...")
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
    print(f"\nRunning continuously. Press Ctrl+C to stop.")

    # ── Continuous loop ────────────────────────────────────────────────────
    pass_num = 0
    try:
        while True:
            pass_num += 1
            run_pass(pass_num, t1, t2, base_url, num_batches)

            cooldown = random.uniform(PASS_COOLDOWN_MIN, PASS_COOLDOWN_MAX)
            print(f"\n  ... Cooldown {cooldown:.1f}s before next pass ...")
            time.sleep(cooldown)

    except KeyboardInterrupt:
        print(f"\n\nStopped after {pass_num} passes.")


if __name__ == "__main__":
    main()
