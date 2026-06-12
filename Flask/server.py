from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import psycopg2
from urllib.request import urlopen, Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError
import json
import gzip
from psycopg2.extras import execute_values
import os
from database import connect_to_database, close_db_connection
from logger import GatewayLogger

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Structured logger (file + console), same pattern as tower_mic
log = GatewayLogger(log_dir='logs', log_file_name='gateway.log', level='DEBUG', console=True)

import csv

# Global mapping for sound classes parsed from support/class_groups.csv
sound_classes = {}
try:
    server_dir = os.path.dirname(os.path.abspath(__file__))
    class_groups_path = os.path.normpath(os.path.join(server_dir, "..", "support", "class_groups.csv"))
    if os.path.exists(class_groups_path):
        with open(class_groups_path, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader) # skip header
            for row in reader:
                if len(row) >= 4:
                    try:
                        y_idx = int(row[0])
                        g_idx = int(row[1])
                        g_name = row[2]
                        d_name = row[3]
                        sound_classes[y_idx] = {
                            "group_index": g_idx,
                            "group_name": g_name,
                            "display_name": d_name
                        }
                    except ValueError:
                        continue
        log.info(f"Loaded {len(sound_classes)} sound classes from CSV")
    else:
        log.warning(f"Sound classes file not found at: {class_groups_path}")
except Exception as e:
    log.error(f"Error loading sound classes from CSV: {e}")

# Mic-check health class IDs (must match node firmware)
HEALTH_OK_CLASS_ID = 1022
HEALTH_ERROR_CLASS_ID = 1023

@app.route("/", methods=["GET"])
def index():
    # Count endpoints
    endpoint_count = sum(1 for rule in app.url_map.iter_rules() if rule.endpoint != 'static')

    # Count nodes and gateways from database
    node_count = 0
    gateway_count = 0
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM nodes")
        node_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM gateways")
        gateway_count = cur.fetchone()[0]
        cur.close()
        conn.close()
    except Exception:
        pass

    return render_template("index.html",
        endpoint_count=endpoint_count,
        node_count=node_count,
        gateway_count=gateway_count
    )

@app.route("/map", methods=["GET"])
def map_view():
    return render_template("map.html")

def update_node_connections(cursor):
    """
    Updates all nodes to link them to the nearest gateway using Euclidean distance.
    This runs efficiently in Postgres without requiring PostGIS.
    """
    cursor.execute("""
        UPDATE nodes n
        SET connected_gateway = (
            SELECT g.gateway_id
            FROM gateways g
            ORDER BY POWER(n.latitude - g.latitude, 2) + POWER(n.longitude - g.longitude, 2) ASC
            LIMIT 1
        )
    """)

@app.route("/nodes", methods=["GET"])
def get_nodes():
    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute("SELECT dev_eui, name, latitude, longitude, altitude, range, connected_gateway, health_status, last_health_check FROM nodes")
        rows = cursor.fetchall()
        
        nodes = []
        for row in rows:
            nodes.append({
                "dev_eui": row[0],
                "name": row[1],
                "latitude": row[2],
                "longitude": row[3],
                "altitude": row[4],
                "range": row[5],
                "connected_gateway": row[6],
                "health_status": row[7],
                "last_health_check": row[8].isoformat() if row[8] else None
            })
        return jsonify(nodes), 200
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Error fetching nodes: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

@app.route("/nodes", methods=["POST"])
def create_node():
    data = request.get_json()
    dev_eui = data.get("dev_eui")
    name = data.get("name")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    altitude = data.get("altitude", 0)
    node_range = data.get("range")

    if dev_eui is None or latitude is None or longitude is None or node_range is None:
         return jsonify({"status": "error", "message": "Missing required fields"}), 400

    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute(
            """
            INSERT INTO nodes (dev_eui, name, latitude, longitude, altitude, range)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (dev_eui, name, latitude, longitude, altitude, node_range)
        )
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"DB insert failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 201

@app.route("/nodes/<dev_eui>", methods=["PUT"])
def update_node(dev_eui):
    data = request.get_json()
    name = data.get("name")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    altitude = data.get("altitude", 0)
    node_range = data.get("range")

    if latitude is None or longitude is None or node_range is None:
         return jsonify({"status": "error", "message": "Missing required fields"}), 400

    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute(
            """
            UPDATE nodes 
            SET name=%s, latitude=%s, longitude=%s, altitude=%s, range=%s
            WHERE dev_eui=%s
            """,
            (name, latitude, longitude, altitude, node_range, dev_eui)
        )
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Node not found"}), 404
            
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"DB update failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 200

@app.route("/nodes/<dev_eui>", methods=["DELETE"])
def delete_node(dev_eui):
    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute("DELETE FROM nodes WHERE dev_eui=%s", (dev_eui,))
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Node not found"}), 404
            
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"DB delete failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 200

@app.route("/uplink", methods=["POST"])
def uplink():
    # ChirpStack sends all event types (up, status, join, ack, txack, log, location)
    # to the same URL. We only care about uplink data events.
    event = request.args.get("event")
    if event != "up":
        return jsonify({"status": "ignored", "event": event}), 200

    try:
        data = request.get_json()
    except Exception as e:
        log.error(f"Failed to parse JSON: {e}")
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400

    # LoRaWAN metadata
    dev_eui   = data.get("deviceInfo", {}).get("devEui")
    timestamp = data.get("time")                        # network-server reception time

    # Decoded payload (populated by decoder.js in TTN/Chirpstack)
    decoded = data.get("object", {})

    # Extract the array of detections. Depending on how ChirpStack wraps it, 
    # detections might be inside 'data' or directly in 'object'
    if "data" in decoded and isinstance(decoded["data"], dict) and "detections" in decoded["data"]:
        detections = decoded["data"].get("detections", [])
    else:
        detections = decoded.get("detections", [])

    if not detections:
        log.warning(f"No detections found in uplink or payload could not be decoded. Object: {decoded}")
        return jsonify({"status": "ok", "message": "No detections to process"}), 200

    # Gateway radio stats (first gateway wins)
    rx_info = data.get("rxInfo", [])
    rssi = snr = None
    if rx_info:
        rssi = rx_info[0].get("rssi")
        snr  = rx_info[0].get("snr")
        
        # Check if gateway location is provided by ChirpStack
        gateway_id = rx_info[0].get("gatewayId")
        location = rx_info[0].get("location")
        
        if gateway_id and location and location.get("latitude") and location.get("longitude"):
            # Upsert the gateway: insert if new, update location + last_seen if existing
            lat = location.get("latitude")
            lon = location.get("longitude")
            alt = location.get("altitude", 0)
            
            try:
                cursor, conn = connect_to_database()
                if cursor and conn:
                    cursor.execute(
                        """
                        INSERT INTO gateways (gateway_id, name, latitude, longitude, altitude, range, last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (gateway_id) DO UPDATE
                        SET latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
                            altitude=EXCLUDED.altitude, last_seen=NOW()
                        RETURNING last_seen
                        """,
                        (gateway_id, gateway_id, lat, lon, alt, 5000)
                    )
                    
                    gw_last_seen_row = cursor.fetchone()
                    gw_last_seen = gw_last_seen_row[0].isoformat() if gw_last_seen_row else None
                    update_node_connections(cursor)
                    conn.commit()
                    log.info(f"Upserted gateway {gateway_id} at {lat}, {lon}")
                    
                    # Emit gateway_seen event so the frontend can update in real-time
                    socketio.emit('gateway_seen', {
                        'gateway_id': gateway_id,
                        'last_seen': gw_last_seen
                    })
                    close_db_connection(cursor, conn)
            except Exception as e:
                log.error(f"Failed to upsert gateway location: {e}")
        elif gateway_id:
            # Gateway is in rxInfo but without location — upsert with last_seen only
            try:
                cursor2, conn2 = connect_to_database()
                if cursor2 and conn2:
                    cursor2.execute(
                        """
                        INSERT INTO gateways (gateway_id, name, latitude, longitude, altitude, range, last_seen)
                        VALUES (%s, %s, 0, 0, 0, 5000, NOW())
                        ON CONFLICT (gateway_id) DO UPDATE
                        SET last_seen=NOW()
                        RETURNING last_seen
                        """,
                        (gateway_id, gateway_id)
                    )
                    gw_last_seen_row = cursor2.fetchone()
                    gw_last_seen = gw_last_seen_row[0].isoformat() if gw_last_seen_row else None
                    conn2.commit()
                    socketio.emit('gateway_seen', {
                        'gateway_id': gateway_id,
                        'last_seen': gw_last_seen
                    })
                    close_db_connection(cursor2, conn2)
            except Exception as e:
                log.error(f"Failed to upsert gateway last_seen: {e}")

    # Prepare batch data
    insert_values = []
    health_update = None  # Will be set if a mic-check detection is found
    for det in detections:
        class_id = det.get("class_id")
        azimuth = det.get("azimuth")
        
        # Skip detections with missing required fields to prevent database insert errors
        if class_id is None or azimuth is None:
            continue
            
        node_time = det.get("node_time")
        
        # Track mic-check health status
        if class_id == HEALTH_OK_CLASS_ID:
            health_update = 'ok'
        elif class_id == HEALTH_ERROR_CLASS_ID:
            health_update = 'error'
        
        insert_values.append((dev_eui, timestamp, class_id, azimuth, node_time, rssi, snr))

    if not insert_values:
        log.warning("No valid detections with azimuth found in uplink.")
        return jsonify({"status": "ok", "message": "No valid detections to process"}), 200

    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        # Use execute_values for efficient bulk insert
        insert_query = """
            INSERT INTO detections
                (dev_eui, timestamp, class_id, azimuth, node_time, rssi, snr)
            VALUES %s
        """
        execute_values(cursor, insert_query, insert_values)
        
        # Update node health status if mic-check detection was received
        if health_update and dev_eui:
            cursor.execute(
                """
                UPDATE nodes
                SET health_status = %s, last_health_check = NOW()
                WHERE dev_eui = %s
                RETURNING last_health_check
                """,
                (health_update, dev_eui)
            )
            health_check_row = cursor.fetchone()
            health_check_time = health_check_row[0].isoformat() if health_check_row else None
        
        conn.commit()
        
        # Emit to connected clients via WebSocket
        emitted_detections = []
        for det in detections:
            if det.get("azimuth") is None:
                continue
            emitted_detections.append({
                "dev_eui": dev_eui,
                "class_id": det.get("class_id"),
                "azimuth": det.get("azimuth"),
                "node_time": det.get("node_time"),
                "timestamp": timestamp,
                "rssi": rssi,
                "snr": snr
            })
        socketio.emit('new_detections', emitted_detections);
        
        # Emit health status update if mic-check was received
        if health_update and dev_eui:
            socketio.emit('node_health', {
                'dev_eui': dev_eui,
                'health_status': health_update,
                'last_health_check': health_check_time
            });
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        log.error(f"Database error during bulk insert: {e.pgerror or e}")
        return jsonify({"status": "error", "message": "Database communication error"}), 500
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Unexpected error during bulk insert: {e}")
        return jsonify({"status": "error", "message": "Unexpected server error"}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok", "inserted": len(insert_values)}), 200

@app.route("/detections/recent", methods=["GET"])
def get_recent_detections():
    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute("""
            SELECT dev_eui, class_id, azimuth, EXTRACT(EPOCH FROM (NOW() - timestamp)) * 1000 AS age_ms, node_time
            FROM detections 
            WHERE timestamp >= NOW() - INTERVAL '300 seconds'
        """)
        rows = cursor.fetchall()
        
        recent = []
        for row in rows:
            recent.append({
                "dev_eui": row[0],
                "class_id": row[1],
                "azimuth": row[2],
                "age_ms": int(row[3]) if row[3] is not None else 0,
                "node_time": row[4]
            })
        return jsonify(recent), 200
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Error fetching recent detections: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

@app.route("/api/sound_classes", methods=["GET"])
def get_sound_classes():
    return jsonify(sound_classes), 200

# ---------------------------------------------------------
# Gateway Endpoints
# ---------------------------------------------------------

@app.route("/gateways", methods=["GET"])
def get_gateways():
    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute("SELECT gateway_id, name, latitude, longitude, altitude, range, last_seen FROM gateways")
        rows = cursor.fetchall()
        
        gateways = []
        for row in rows:
            gateways.append({
                "gateway_id": row[0],
                "name": row[1],
                "latitude": row[2],
                "longitude": row[3],
                "altitude": row[4],
                "range": row[5],
                "last_seen": row[6].isoformat() if row[6] else None
            })
        return jsonify(gateways), 200
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Error fetching gateways: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

@app.route("/gateways", methods=["POST"])
def create_gateway():
    data = request.get_json()
    gateway_id = data.get("gateway_id")
    name = data.get("name")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    altitude = data.get("altitude", 0)
    gateway_range = data.get("range")

    if gateway_id is None or latitude is None or longitude is None or gateway_range is None:
         return jsonify({"status": "error", "message": "Missing required fields"}), 400

    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute(
            """
            INSERT INTO gateways (gateway_id, name, latitude, longitude, altitude, range)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (gateway_id) DO UPDATE
            SET name=EXCLUDED.name, latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
                altitude=EXCLUDED.altitude, range=EXCLUDED.range
            """,
            (gateway_id, name, latitude, longitude, altitude, gateway_range)
        )
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"DB insert failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 201

@app.route("/gateways/<gateway_id>", methods=["PUT"])
def update_gateway(gateway_id):
    data = request.get_json()
    name = data.get("name")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    altitude = data.get("altitude", 0)
    gateway_range = data.get("range")

    if latitude is None or longitude is None or gateway_range is None:
         return jsonify({"status": "error", "message": "Missing required fields"}), 400

    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute(
            """
            UPDATE gateways 
            SET name=%s, latitude=%s, longitude=%s, altitude=%s, range=%s
            WHERE gateway_id=%s
            """,
            (name, latitude, longitude, altitude, gateway_range, gateway_id)
        )
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Gateway not found"}), 404
            
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"DB update failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 200

@app.route("/gateways/<gateway_id>", methods=["DELETE"])
def delete_gateway(gateway_id):
    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        cursor.execute("DELETE FROM gateways WHERE gateway_id=%s", (gateway_id,))
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Gateway not found"}), 404
            
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"DB delete failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 200

# ---------------------------------------------------------
# ADS-B Proxy (bypasses browser CORS restrictions)
# ---------------------------------------------------------

@app.route("/api/adsb", methods=["GET"])
def adsb_proxy():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    dist = request.args.get("dist", "100")

    if lat is None or lon is None:
        return jsonify({"status": "error", "message": "Missing lat/lon parameters"}), 400

    try:
        lat_f = float(lat)
        lon_f = float(lon)
        dist_i = min(int(float(dist)), 250)  # cap at 250nm
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid parameters"}), 400

    url = f"https://api.adsb.lol/v2/lat/{lat_f:.4f}/lon/{lon_f:.4f}/dist/{dist_i}"
    try:
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return jsonify(data), 200
    except URLError as e:
        log.error(f"ADS-B API fetch failed: {e}")
        return jsonify({"status": "error", "message": "Failed to fetch ADS-B data"}), 502
    except Exception as e:
        log.error(f"ADS-B proxy error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/adsb/track/<icao24>", methods=["GET"])
def adsb_track_proxy(icao24):
    """Proxy for aircraft trail data.
    Tries globe.adsb.lol (full trace) first, then OpenSky."""
    import ssl

    hex_lower = icao24.lower()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 DIVSGateway/1.0"

    def parse_globe_trace(trace_data):
        """Parse globe.adsb.lol trace format into our path format."""
        path = []
        for point in trace_data:
            if len(point) < 3:
                continue
            lat = point[1]
            lon = point[2]
            alt_baro = point[3] if len(point) > 3 else None
            track_deg = point[5] if len(point) > 5 else None
            ts = point[0] if len(point) > 0 else 0
            if lat is not None and lon is not None:
                alt_m = None
                if isinstance(alt_baro, (int, float)):
                    alt_m = alt_baro * 0.3048
                path.append([ts, lat, lon, alt_m, track_deg, alt_baro == "ground"])
        return path

    # Try 1: globe.adsb.lol trace JSON (full flight history)
    last2 = hex_lower[-2:]
    trace_url = f"https://globe.adsb.lol/data/traces/{last2}/trace_full_{hex_lower}.json"
    try:
        log.info(f"track: trying globe trace for {hex_lower}")
        req = Request(trace_url, headers={
            "User-Agent": ua,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"
        })
        with urlopen(req, timeout=15, context=ctx) as resp:
            raw_bytes = resp.read()
            # Handle gzip-compressed responses (0x1f 0x8b = gzip magic)
            if raw_bytes[:2] == b'\x1f\x8b':
                raw_bytes = gzip.decompress(raw_bytes)
            raw = raw_bytes.decode('utf-8')
            log.info(f"track: globe returned {len(raw)} bytes (status {resp.getcode()})")
            if raw.strip():
                trace_data = json.loads(raw)
                if "trace" in trace_data and len(trace_data["trace"]) >= 2:
                    path = parse_globe_trace(trace_data["trace"])
                    if len(path) >= 2:
                        log.info(f"track: globe returned {len(path)} points for {hex_lower}")
                        return jsonify({"icao24": hex_lower, "path": path}), 200
    except Exception as e:
        log.warning(f"track: globe failed for {hex_lower}: {type(e).__name__}: {e}")

    # Try 2: OpenSky Network
    try:
        url = f"https://opensky-network.org/api/tracks/all?icao24={hex_lower}&time=0"
        log.info(f"track: trying OpenSky for {hex_lower}")
        req = Request(url, headers={"User-Agent": ua})
        with urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        log.info(f"track: OpenSky returned {len(data.get('path', []))} points")
        return jsonify(data), 200
    except Exception as e:
        log.warning(f"track: OpenSky failed for {hex_lower}: {type(e).__name__}: {e}")

    return jsonify({"status": "error", "message": "No trail data available"}), 502

@app.route("/api/adsb/routeset", methods=["POST"])
def adsb_routeset_proxy():
    """Proxy to routeset API for aircraft route data.
    Tries adsb.lol first, falls back to adsb.im."""
    try:
        body = request.get_json(force=True)
        log.info(f"routeset request body: {json.dumps(body)}")
        encoded = json.dumps(body).encode("utf-8")

        # Try multiple routeset API providers
        providers = [
            "https://api.adsb.lol/api/0/routeset",
            "https://adsb.im/api/0/routeset",
        ]

        last_error = None
        for api_url in providers:
            try:
                req = Request(
                    api_url,
                    data=encoded,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urlopen(req, timeout=10) as resp:
                    status = resp.getcode()
                    raw = resp.read().decode()
                    log.info(f"routeset [{api_url}] status={status}, body(500)={raw[:500]}")

                    if not raw or not raw.strip():
                        log.warning(f"routeset [{api_url}] returned empty body")
                        last_error = "Empty response"
                        continue

                    data = json.loads(raw)
                    return jsonify(data), 200
            except Exception as e:
                log.warning(f"routeset [{api_url}] failed: {e}")
                last_error = str(e)
                continue

        return jsonify({"status": "error", "message": f"All routeset providers failed: {last_error}"}), 502
    except Exception as e:
        log.error(f"routeset proxy error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
