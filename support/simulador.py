import os
import math
import time
import requests
import sys
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv
import psycopg2

print("="*60)
print(" Starting DIVS DOA Mic Tower Multi-Node Simulator...")
print("="*60)

# Load environment variables
script_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(script_dir, "..", ".env")
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    load_dotenv()

# Setup URL
URL = os.getenv("GATEWAY_URL", "http://localhost/uplink?event=up")

# Spherical Earth geometry helpers
def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(d_lon)
    bearing = math.atan2(y, x)
    return (math.degrees(bearing) + 360) % 360

def get_db_connection():
    host = os.getenv('POSTGRES_HOST', 'localhost')
    # If host env is 'postgres' (docker-compose) but script runs on host, fallback to localhost
    if host == 'postgres' and not os.path.exists('/.dockerenv'):
        host = 'localhost'
        
    return psycopg2.connect(
        database=os.getenv('POSTGRES_DB', 'postgres'),
        user=os.getenv('POSTGRES_USER', 'postgres'),
        password=os.getenv('POSTGRES_PASSWORD', 'postgres'),
        host=host,
        port=os.getenv('POSTGRES_PORT', '5432')
    )

def fetch_node_coords_live(dev_eui_list):
    """
    Fetches the live coordinates of the given dev_eui's from the database.
    """
    if not dev_eui_list:
        return {}
        
    coords = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT dev_eui, latitude, longitude, range, name FROM nodes WHERE dev_eui IN %s",
            (tuple(dev_eui_list),)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for r in rows:
            coords[r[0]] = {
                "latitude": r[1],
                "longitude": r[2],
                "range": r[3],
                "name": r[4]
            }
    except Exception:
        # DB failure during loop, ignore and use previous coordinates
        pass
    return coords

def main():
    # Parse CLI Arguments
    parser = argparse.ArgumentParser(description="DIVS DOA Mic Tower Multi-Node Simulator")
    parser.add_argument("-n", "--nodes", type=int, help="Number of nodes to simulate")
    parser.add_argument("-u", "--url", type=str, default=URL, help="Gateway uplink URL")
    parser.add_argument("-p", "--period", type=float, default=120.0, help="Simulation target sweep period in seconds")
    parser.add_argument("-i", "--interval", type=float, default=0.5, help="Step time interval in seconds")
    args = parser.parse_args()

    target_url = args.url
    print(f"Uplink Target URL: {target_url}")
    print(f"Sweep Period: {args.period}s | Step Interval: {args.interval}s")

    # Determine desired nodes to simulate
    num_nodes = args.nodes
    if num_nodes is None:
        num_nodes_env = os.getenv("SIMULATOR_NUM_NODES")
        if num_nodes_env:
            try:
                num_nodes = int(num_nodes_env)
            except ValueError:
                pass

    # TTY Interactive selection
    if num_nodes is None:
        if sys.stdin.isatty():
            existing = []
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT dev_eui, name, latitude, longitude FROM nodes ORDER BY dev_eui ASC")
                existing = cur.fetchall()
                cur.close()
                conn.close()
            except Exception as e:
                print(f"Could not read nodes for prompt: {e}")
            
            print("\n" + "="*50)
            print(" DIVS Simulator - Interactive Node Selection")
            print("="*50)
            if existing:
                print(f"Registered towers currently in the DB ({len(existing)}):")
                for idx, row in enumerate(existing):
                    print(f"  [{idx + 1}] EUI: {row[0]} | Name: {row[1] or 'Unnamed'} ({round(row[2], 5)}, {round(row[3], 5)})")
            else:
                print("No registered towers found in the database.")
            
            print("\nSelect the number of towers you want to simulate:")
            print("  - Enter a number (e.g. 3) to simulate that many towers.")
            print("  - If the DB has fewer nodes, the missing ones will be auto-registered.")
            print("  - Press Enter to use the default (2 nodes).")
            
            try:
                sel = input("\nNumber of towers (default: 2): ").strip()
                if sel == "":
                    num_nodes = 2
                else:
                    num_nodes = int(sel)
                    if num_nodes <= 0:
                        num_nodes = 2
            except (KeyboardInterrupt, EOFError, ValueError):
                num_nodes = 2
        else:
            num_nodes = 2

    # Fetch currently registered nodes to reuse
    registered_nodes = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT dev_eui, name, latitude, longitude, range FROM nodes ORDER BY dev_eui ASC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        registered_nodes = [
            {"dev_eui": r[0], "name": r[1], "latitude": r[2], "longitude": r[3], "range": r[4]}
            for r in rows
        ]
    except Exception as e:
        print(f"DB read warning: {e}")

    # Build list of nodes to simulate
    simulated_nodes = []
    for i in range(min(num_nodes, len(registered_nodes))):
        simulated_nodes.append(registered_nodes[i])

    # Generate synthetic nodes if we need more than currently in DB
    missing_count = num_nodes - len(simulated_nodes)
    if missing_count > 0:
        print(f"\nAuto-generating {missing_count} synthetic towers to reach requested count of {num_nodes}...")
        center_lat = 40.9448
        center_lon = -8.4082
        radius = 0.0015  # ~150 meters circular layout
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            for i in range(missing_count):
                node_idx = len(simulated_nodes) + 1
                dev_eui = f"040922192026{node_idx:04d}"
                name = f"Sim Tower {node_idx}"
                
                # Arrange in a perfect circle ring centered at Aerodromo
                angle = 2.0 * math.pi * (node_idx - 1) / num_nodes
                lat = center_lat + radius * math.cos(angle)
                lon = center_lon + radius * math.sin(angle)
                node_range = 5000.0
                altitude = 10.0
                
                cur.execute("""
                    INSERT INTO nodes (dev_eui, name, latitude, longitude, altitude, range)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (dev_eui) DO UPDATE 
                    SET name = EXCLUDED.name,
                        latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude,
                        altitude = EXCLUDED.altitude,
                        range = EXCLUDED.range
                """, (dev_eui, name, lat, lon, altitude, node_range))
                
                synthetic_node = {
                    "dev_eui": dev_eui,
                    "name": name,
                    "latitude": lat,
                    "longitude": lon,
                    "range": node_range
                }
                simulated_nodes.append(synthetic_node)
                print(f"  Registered: {name} (EUI: {dev_eui}) at {round(lat, 6)}, {round(lon, 6)}")
            
            # Connect generated nodes to gateways if any exist
            cur.execute("""
                UPDATE nodes n
                SET connected_gateway = (
                    SELECT g.gateway_id
                    FROM gateways g
                    ORDER BY POWER(n.latitude - g.latitude, 2) + POWER(n.longitude - g.longitude, 2) ASC
                    LIMIT 1
                )
                WHERE connected_gateway IS NULL OR connected_gateway = ''
            """)
            
            conn.commit()
            cur.close()
            conn.close()
            print("Successfully synchronized database.")
        except Exception as e:
            print(f"Warning: Could not sync synthetic nodes into DB: {e}")
            # Fallback to in-memory generation if DB write failed
            for i in range(missing_count):
                node_idx = len(simulated_nodes) + 1
                dev_eui = f"040922192026{node_idx:04d}"
                name = f"Sim Tower {node_idx}"
                angle = 2.0 * math.pi * (node_idx - 1) / num_nodes
                lat = center_lat + radius * math.cos(angle)
                lon = center_lon + radius * math.sin(angle)
                simulated_nodes.append({
                    "dev_eui": dev_eui,
                    "name": name,
                    "latitude": lat,
                    "longitude": lon,
                    "range": 5000.0
                })

    print(f"\nSimulation configured for {len(simulated_nodes)} towers:")
    for n in simulated_nodes:
        print(f"  🗼 {n['name']} (EUI: {n['dev_eui']})")
    print("="*60 + "\n")

    # Main simulation loop
    t = 0.0
    R = 6378137.0  # Earth's radius in meters
    
    while True:
        # Fetch live node positions from DB in case user dragged them on Leaflet map
        dev_euis = [n["dev_eui"] for n in simulated_nodes]
        live_coords = fetch_node_coords_live(dev_euis)
        for node in simulated_nodes:
            eui = node["dev_eui"]
            if eui in live_coords:
                node["latitude"] = live_coords[eui]["latitude"]
                node["longitude"] = live_coords[eui]["longitude"]
                node["range"] = live_coords[eui]["range"]
                node["name"] = live_coords[eui]["name"]

        # Calculate target position
        if len(simulated_nodes) >= 2:
            # 1. Centroid of all towers
            lat_mid = sum(n["latitude"] for n in simulated_nodes) / len(simulated_nodes)
            lon_mid = sum(n["longitude"] for n in simulated_nodes) / len(simulated_nodes)
            
            # 2. Find the tower furthest from the centroid to define our sweep axis and scale
            furthest_node = simulated_nodes[0]
            max_d = 0.0
            for node in simulated_nodes:
                dy = (node["latitude"] - lat_mid) * math.pi / 180.0 * R
                dx = (node["longitude"] - lon_mid) * math.pi / 180.0 * R * math.cos(lat_mid * math.pi / 180.0)
                dist = math.sqrt(dx**2 + dy**2)
                if dist > max_d:
                    max_d = dist
                    furthest_node = node
                    
            if max_d < 1e-3:
                max_d = 150.0  # Fallback radius if all nodes are on top of each other
                
            # Vector from midpoint to furthest tower defines the orientation of our Figure-8
            dy_axis = (furthest_node["latitude"] - lat_mid) * math.pi / 180.0 * R
            dx_axis = (furthest_node["longitude"] - lon_mid) * math.pi / 180.0 * R * math.cos(lat_mid * math.pi / 180.0)
            D_axis = math.sqrt(dx_axis**2 + dy_axis**2)
            
            if D_axis < 1e-3:
                ex = 1.0
                ey = 0.0
            else:
                ex = dx_axis / D_axis
                ey = dy_axis / D_axis
                
            px = -ey
            py = ex
            
            # 3. Dynamic amplitude: 1.5x the max distance from the midpoint (so it sweeps between and around all towers)
            # but capped at 90% of average tower range so the target doesn't wander off the map completely
            avg_range = sum(n.get("range", 5000.0) for n in simulated_nodes) / len(simulated_nodes)
            A = min(max_d * 1.5, avg_range * 0.9)
            
            # Lemniscate of Bernoulli formulas
            sin_t = math.sin(t)
            cos_t = math.cos(t)
            denom = 1.0 + sin_t**2
            
            u = (A * cos_t) / denom
            v = (A * sin_t * cos_t) / denom
            
            x_offset = u * ex + v * px
            y_offset = u * ey + v * py
            
            lat_t = lat_mid + y_offset / R * 180.0 / math.pi
            lon_t = lon_mid + x_offset / (R * math.cos(lat_mid * math.pi / 180.0)) * 180.0 / math.pi
        else:
            # Single tower orbit fallback
            node_A = simulated_nodes[0]
            lat_mid, lon_mid = node_A["latitude"], node_A["longitude"]
            orbit_radius = 150.0
            
            x_offset = orbit_radius * math.cos(t)
            y_offset = orbit_radius * math.sin(t)
            
            lat_t = lat_mid + y_offset / R * 180.0 / math.pi
            lon_t = lon_mid + x_offset / (R * math.cos(lat_mid * math.pi / 180.0)) * 180.0 / math.pi

        # Broadcast simulated bearings from all nodes
        current_time_iso = datetime.now(timezone.utc).isoformat()
        secs_since_midnight = int(time.time() % 86400)
        
        print(f"Time: {datetime.now().strftime('%H:%M:%S')} | Target Center: {round(lat_mid, 6)}, {round(lon_mid, 6)}")
        for i, node in enumerate(simulated_nodes):
            lat_node = node["latitude"]
            lon_node = node["longitude"]
            eui = node["dev_eui"]
            name = node["name"] or f"Tower {i+1}"
            
            azimuth = calculate_bearing(lat_node, lon_node, lat_t, lon_t)
            
            # Sound alarm types (390: Siren, 391: Civil Defense, 392: Buzzer, 394: Fire Alarm, etc.)
            type_code = 390 + (i % 7)
            
            payload = {
                "deviceInfo": {"devEui": eui},
                "time": current_time_iso,
                "object": {
                    "detections": [{
                        "type_code": type_code,
                        "azimuth": round(azimuth, 2),
                        "secs_since_midnight": secs_since_midnight
                    }]
                },
                "rxInfo": [{"rssi": -80.0 - i, "snr": 10.0 - (i * 0.5)}]
            }
            
            try:
                res = requests.post(target_url, json=payload, timeout=2.0)
                eui_short = eui[:8] if len(eui) > 8 else eui
                print(f"  Sent {name} ({eui_short}) azimuth = {round(azimuth, 1)}° | Alarm Code: {type_code} → Status: {res.status_code}")
            except Exception as e:
                print(f"  Failed to post {name} uplink: {e}")
                
        print(f"Path parameter t: {round(t, 2)} rad | Target Position: {round(lat_t, 6)}, {round(lon_t, 6)}")
        print("-" * 65)

        # Progress lemniscate sweep step
        t = (t + (2.0 * math.pi / (args.period / args.interval))) % (2.0 * math.pi)
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
