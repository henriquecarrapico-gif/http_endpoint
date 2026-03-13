from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import execute_values
import os

app = Flask(__name__)

# PostgreSQL connection settings
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "chirpstack")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")

conn = psycopg2.connect(
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD
)

@app.route("/uplink", methods=["POST"])
def uplink():
    data = request.get_json()

    # LoRaWAN metadata
    dev_eui   = data.get("deviceInfo", {}).get("devEui")
    timestamp = data.get("time")                        # network-server reception time

    # Decoded payload (populated by decoder.js in TTN/Chirpstack)
    decoded        = data.get("object", {})
    type_code      = decoded.get("type_code")           # raw number — you decode it later
    azimuth        = decoded.get("azimuth")
    node_timestamp = decoded.get("secs_since_midnight")

    # Gateway radio stats (first gateway wins)
    rx_info = data.get("rxInfo", [])
    rssi = snr = None
    if rx_info:
        rssi = rx_info[0].get("rssi")
        snr  = rx_info[0].get("snr")

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO detections
                    (dev_eui, timestamp, type_code, azimuth, node_timestamp, rssi, snr)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (dev_eui, timestamp, type_code, azimuth, node_timestamp, rssi, snr)
            )
            conn.commit()
    except Exception as e:
        conn.rollback()
        app.logger.error(f"DB insert failed, rolled back: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
