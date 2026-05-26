-- Detections table for LoRaWAN object detection uplinks.
-- class_id is the raw YAMNet class index (0-520). Decode to group using class_groups.csv.
-- node_time is fractional seconds-since-midnight UTC as reported by the node (0.1s precision).
-- timestamp is the network-server reception time (from LoRaWAN metadata).

CREATE TABLE IF NOT EXISTS nodes (
    dev_eui VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude DOUBLE PRECISION,
    range DOUBLE PRECISION,
    connected_gateway VARCHAR(255), -- Note: No strict FOREIGN KEY constraint allows nodes without gateways
    health_status VARCHAR(10) DEFAULT 'unknown', -- 'ok', 'error', or 'unknown'
    last_health_check TIMESTAMPTZ              -- timestamp of last mic-check detection
);

CREATE TABLE IF NOT EXISTS gateways (
    gateway_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude DOUBLE PRECISION,
    range DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS detections (
    id               SERIAL PRIMARY KEY,
    dev_eui          VARCHAR(16)  NOT NULL,   -- REFERENCES nodes(dev_eui),
    timestamp        TIMESTAMPTZ  NOT NULL,   -- network server reception time
    class_id         INTEGER      NOT NULL,   -- raw YAMNet class index (0-520), 1022=mic OK, 1023=mic error
    azimuth          REAL         NOT NULL,   -- degrees, 0.0–359.65
    node_time        REAL         NOT NULL,   -- seconds since midnight UTC with 0.1s precision (node clock)
    rssi             REAL,
    snr              REAL
);

CREATE INDEX IF NOT EXISTS idx_detections_dev_eui    ON detections (dev_eui);
CREATE INDEX IF NOT EXISTS idx_detections_timestamp  ON detections (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_detections_class_id   ON detections (class_id);
