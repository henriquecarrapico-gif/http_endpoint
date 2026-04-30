from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import psycopg2
from psycopg2.extras import execute_values
import os
from database import connect_to_database, close_db_connection

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

@app.route("/", methods=["GET"])
def index():
    endpoints = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint != 'static':
            methods = ', '.join(sorted([m for m in rule.methods if m not in ['OPTIONS', 'HEAD']]))
            endpoints[rule.rule] = f"Methods: {methods}"
            
    return jsonify({
        "service": "DIVS Gateway HTTP Endpoint",
        "description": "Flask API for handling Chirpstack integrations",
        "endpoints": endpoints
    }), 200

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

        cursor.execute("SELECT dev_eui, name, latitude, longitude, altitude, range, connected_gateway FROM nodes")
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
                "connected_gateway": row[6]
            })
        return jsonify(nodes), 200
    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Error fetching nodes: {e}")
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

    if not all([dev_eui, latitude, longitude, node_range]):
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
        app.logger.error(f"DB insert failed, rolled back: {e}")
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

    if not all([latitude, longitude, node_range]):
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
        app.logger.error(f"DB update failed, rolled back: {e}")
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
        app.logger.error(f"DB delete failed, rolled back: {e}")
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
        app.logger.error(f"Failed to parse JSON: {e}")
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
        app.logger.warning(f"No detections found in uplink or payload could not be decoded. Object: {decoded}")
        return jsonify({"status": "ok", "message": "No detections to process"}), 200

    # Gateway radio stats (first gateway wins)
    rx_info = data.get("rxInfo", [])
    rssi = snr = None
    if rx_info:
        rssi = rx_info[0].get("rssi")
        snr  = rx_info[0].get("snr")

    # Prepare batch data
    insert_values = []
    for det in detections:
        type_code = det.get("type_code")
        azimuth = det.get("azimuth")
        
        # Skip detections with missing azimuth to prevent database insert errors
        if azimuth is None:
            continue
            
        node_timestamp = det.get("secs_since_midnight")
        
        insert_values.append((dev_eui, timestamp, type_code, azimuth, node_timestamp, rssi, snr))

    if not insert_values:
        app.logger.warning("No valid detections with azimuth found in uplink.")
        return jsonify({"status": "ok", "message": "No valid detections to process"}), 200

    cursor, conn = None, None
    try:
        cursor, conn = connect_to_database()
        if not conn or not cursor:
            return jsonify({"status": "error", "message": "Database connection failed"}), 500

        # Use execute_values for efficient bulk insert
        insert_query = """
            INSERT INTO detections
                (dev_eui, timestamp, type_code, azimuth, node_timestamp, rssi, snr)
            VALUES %s
        """
        execute_values(cursor, insert_query, insert_values)
        conn.commit()
        
        # Emit to connected clients via WebSocket
        emitted_detections = []
        for det in detections:
            if det.get("azimuth") is None:
                continue
            emitted_detections.append({
                "dev_eui": dev_eui,
                "type_code": det.get("type_code"),
                "azimuth": det.get("azimuth")
            })
        socketio.emit('new_detections', emitted_detections)
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Database error during bulk insert: {e.pgerror or e}")
        return jsonify({"status": "error", "message": "Database communication error"}), 500
    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Unexpected error during bulk insert: {e}")
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
            SELECT dev_eui, type_code, azimuth, EXTRACT(EPOCH FROM (NOW() - timestamp)) * 1000 AS age_ms
            FROM detections 
            WHERE timestamp >= NOW() - INTERVAL '60 seconds'
        """)
        rows = cursor.fetchall()
        
        recent = []
        for row in rows:
            recent.append({
                "dev_eui": row[0],
                "type_code": row[1],
                "azimuth": row[2],
                "age_ms": int(row[3]) if row[3] is not None else 0
            })
        return jsonify(recent), 200
    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Error fetching recent detections: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

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

        cursor.execute("SELECT gateway_id, name, latitude, longitude, altitude, range FROM gateways")
        rows = cursor.fetchall()
        
        gateways = []
        for row in rows:
            gateways.append({
                "gateway_id": row[0],
                "name": row[1],
                "latitude": row[2],
                "longitude": row[3],
                "altitude": row[4],
                "range": row[5]
            })
        return jsonify(gateways), 200
    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Error fetching gateways: {e}")
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

    if not all([gateway_id, latitude, longitude, gateway_range]):
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
            """,
            (gateway_id, name, latitude, longitude, altitude, gateway_range)
        )
        update_node_connections(cursor)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"DB insert failed, rolled back: {e}")
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

    if not all([latitude, longitude, gateway_range]):
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
        app.logger.error(f"DB update failed, rolled back: {e}")
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
        app.logger.error(f"DB delete failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor and conn:
            close_db_connection(cursor, conn)

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
