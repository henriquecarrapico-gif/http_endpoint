function decodeUplink(input) {
    var b = input.bytes;

    // Each detection is 5 bytes (40 bits): [azimuth:10][class_id:10][node_time:20]
    if (b.length % 5 !== 0 || b.length === 0) {
        return { errors: ["payload size invalid: expected multiple of 5 bytes, got " + b.length] };
    }

    var detections = [];

    for (var i = 0; i < b.length; i += 5) {

        // Reassemble 5 bytes into a 40-bit value using hi byte + lo 32-bit word
        var hi = b[i];
        var lo = ((b[i + 1] << 24) | (b[i + 2] << 16) | (b[i + 3] << 8) | b[i + 4]) >>> 0;

        // Azimuth is the top 10 bits (bits 30-39): hi >> 2
        var azimuthRaw = (hi >>> 2) & 0x3FF;

        // Class ID is the next 10 bits (bits 20-29): bottom 2 of hi + top 8 of lo
        var classId = ((hi & 0x03) << 8) | (lo >>> 24);

        // Node time is the bottom 20 bits (bits 0-19) in deciseconds
        var deciSecs = lo & 0xFFFFF;

        detections.push({
            class_id:  classId,
            azimuth:   azimuthRaw * 360.0 / 1024.0,
            node_time: deciSecs / 10.0
        });
    }

    return {
        data: {
            detections: detections
        }
    };
}
