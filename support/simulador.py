import requests
import time
from datetime import datetime, timezone
print("Starting simulator...")
URL = "http://divsgateway0.local/uplink?event=up"

dev_eui = "0409221920260001"

azimuth = 0  # starting angle
typecode = 0
while True:
    payload = {
        "deviceInfo": {"devEui": dev_eui},
        "time": datetime.now(timezone.utc).isoformat(),  #dynamic time
        "object": {
            "detections": [
            {
                "type_code": typecode,
                "azimuth": azimuth,  # dynamic azimuth
                "secs_since_midnight": int(time.time() % 86400)
            }
        ]
    },
    "rxInfo": [{"rssi": -90, "snr": 5.5}]

}

    try:
        response = requests.post(URL, json=payload)
        print(f"Sent azimuth={azimuth} → Status: {response.status_code}")
    except Exception as e:
        print("Error:", e)

    # Increase azimuth
    azimuth = (azimuth + 30) % 360  # wraps at 360
    typecode = (typecode + 8) % 63  # cycle through type codes 0-4
    time.sleep(1)  # send every 3 second
