"""
DIVS Uplink Receiver — Lightweight, always-on microservice.

Handles ONLY the POST /uplink endpoint from ChirpStack so that
detection data keeps flowing into postgres even when the main
Flask dashboard/server is shut down for maintenance.
"""

from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import execute_values
from database import connect_to_database, close_db_connection
from logger import GatewayLogger

app = Flask(__name__)

log = GatewayLogger(log_dir='logs', log_file_name='uplink.log', level='DEBUG', console=True)

# Mic-check health class IDs (must match node firmware)
HEALTH_OK_CLASS_ID = 1022
HEALTH_ERROR_CLASS_ID = 1023


def update_node_connections(cursor):
    """
    Updates all nodes to link them to the nearest gateway using Euclidean distance.
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
                        """,
                        (gateway_id, gateway_id, lat, lon, alt, 5000)
                    )
                    update_node_connections(cursor)
                    conn.commit()
                    log.info(f"Upserted gateway {gateway_id} at {lat}, {lon}")
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
                        """,
                        (gateway_id, gateway_id)
                    )
                    conn2.commit()
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
                """,
                (health_update, dev_eui)
            )

        conn.commit()
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


@app.route("/health", methods=["GET"])
def health():
    """Simple health-check for monitoring."""
    return jsonify({"status": "ok", "service": "uplink-receiver"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
