import os
import math
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import psycopg2

print("Starting DIVS DOA Mic Tower Simulator...")

# Load environment variables
script_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(script_dir, "..", ".env")
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    load_dotenv()

# Setup URL
URL = os.getenv("GATEWAY_URL", "http://divsgateway0.local/uplink?event=up")
print(f"Uplink Target URL: {URL}")

# Nodes to simulate
NODE_A_EUI = "0409221920260001"
NODE_B_EUI = "0409221920260002"

# Spherical Earth geometry helpers
def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(d_lon)
    bearing = math.atan2(y, x)
    return (math.degrees(bearing) + 360) % 360

def get_destination_point(lat, lon, bearing_deg, distance_m):
    R = 6378137.0  # Earth's radius in meters
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d_rad = distance_m / R
    
    lat2 = math.asin(math.sin(lat1) * math.cos(d_rad) +
                     math.cos(lat1) * math.sin(d_rad) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(d_rad) * math.cos(lat1),
                             math.cos(d_rad) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)

def fetch_node_coordinates():
    host = os.getenv('POSTGRES_HOST', 'localhost')
    if host == 'postgres':
        # Running on host outside docker network
        host = 'localhost'
        
    try:
        connection = psycopg2.connect(
            database=os.getenv('POSTGRES_DB', 'postgres'),
            user=os.getenv('POSTGRES_USER', 'postgres'),
            password=os.getenv('POSTGRES_PASSWORD', 'postgres'),
            host=host,
            port=os.getenv('POSTGRES_PORT', '5432')
        )
        cursor = connection.cursor()
        
        # Retrieve Node A coordinates
        cursor.execute("SELECT latitude, longitude FROM nodes WHERE dev_eui = %s", (NODE_A_EUI,))
        node_a = cursor.fetchone()
        
        # Retrieve Node B coordinates
        cursor.execute("SELECT latitude, longitude FROM nodes WHERE dev_eui = %s", (NODE_B_EUI,))
        node_b = cursor.fetchone()
        
        cursor.close()
        connection.close()
        
        if node_a and node_b:
            return node_a[0], node_a[1], node_b[0], node_b[1]
    except Exception as e:
        print("Database query warning:", e)
        
    # Standard Fallback coordinates centered on Aerodromo
    return 40.94490246, -8.40818614, 40.94475254, -8.40817004

# Main simulation loop
orbit_angle = 0.0

while True:
    # 1. Fetch node positions dynamically from DB to pick up any UI drag updates
    lat_A, lon_A, lat_B, lon_B = fetch_node_coordinates()
    
    # 2. Compute midpoint as the target center
    lat_mid = (lat_A + lat_B) / 2.0
    lon_mid = (lon_A + lon_B) / 2.0
    
    # 3. Calculate simulated moving target position (50m circular path)
    lat_t, lon_t = get_destination_point(lat_mid, lon_mid, orbit_angle, 50.0)
    
    # 4. Calculate exact mathematical bearings from each node to target
    azimuth_A = calculate_bearing(lat_A, lon_A, lat_t, lon_t)
    azimuth_B = calculate_bearing(lat_B, lon_B, lat_t, lon_t)
    
    current_time_iso = datetime.now(timezone.utc).isoformat()
    secs_since_midnight = int(time.time() % 86400)
    
    # 5. Formulate ChirpStack payloads
    # Node A detects Type 43 (Siren), Node B detects Type 44 (Civil Defense Siren)
    payload_A = {
        "deviceInfo": {"devEui": NODE_A_EUI},
        "time": current_time_iso,
        "object": {
            "detections": [{
                "type_code": 43,
                "azimuth": round(azimuth_A, 2),
                "secs_since_midnight": secs_since_midnight
            }]
        },
        "rxInfo": [{"rssi": -85.0, "snr": 8.0}]
    }
    
    payload_B = {
        "deviceInfo": {"devEui": NODE_B_EUI},
        "time": current_time_iso,
        "object": {
            "detections": [{
                "type_code": 44,
                "azimuth": round(azimuth_B, 2),
                "secs_since_midnight": secs_since_midnight
            }]
        },
        "rxInfo": [{"rssi": -90.0, "snr": 6.0}]
    }
    
    # 6. Post uplinks in parallel (consecutively)
    try:
        res_A = requests.post(URL, json=payload_A)
        print(f"Sent Node A (Este) azimuth = {round(azimuth_A, 1)}° → Status: {res_A.status_code}")
    except Exception as e:
        print("Failed to post Node A uplink:", e)
        
    try:
        res_B = requests.post(URL, json=payload_B)
        print(f"Sent Node B (Sul)  azimuth = {round(azimuth_B, 1)}° → Status: {res_B.status_code}")
    except Exception as e:
        print("Failed to post Node B uplink:", e)
        
    print(f"Orbit angle: {int(orbit_angle)}° | Target: {round(lat_t, 6)}, {round(lon_t, 6)}")
    print("-" * 50)
    
    # 7. Progress orbit (12 degrees per second = 30 second period)
    orbit_angle = (orbit_angle + 12.0) % 360.0
    time.sleep(1.0)
