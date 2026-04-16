/*
 * FridgeGuard — ESP32 BLE Scanner + DHT11
 *
 * Scans for 16-bit BLE service UUIDs broadcast by roommates' phones
 * via nRF Connect. Sends results to the Raspberry Pi over USB serial.
 *
 * Each roommate configures nRF Connect to advertise a unique 16-bit
 * service UUID (e.g. 0xFF01, 0xFF02, 0xFF03).
 *
 * Serial protocol (JSON, one line per message):
 *   BLE:  {"type":"ble","ts":12345,"devices":[{"uuid":"ff01","rssi":-42}]}
 *   Temp: {"type":"temp","temp_c":6.0,"humidity":45,"ts":12345}
 */

#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <ArduinoJson.h>

// --- ADD DHT LIBRARIES & CONFIG ---
#include <DHT.h>

#define DHTPIN 13      // Pin connected to the DHT sensor
#define DHTTYPE DHT11  // Using a DHT11 sensor

DHT dht(DHTPIN, DHTTYPE); // Initialize the DHT sensor

// ── CONFIG ──────────────────────────────────────────
const int    NUM_TARGETS   = 3;
const int    RSSI_FLOOR    = -100;
const int    CONFIRM_SCANS = 1;
const int    SCAN_SECONDS  = 1;

// 16-bit service UUIDs to look for (must match config.yaml)
const uint16_t TARGET_UUIDS[NUM_TARGETS] = {
    0xFF01,   // Satvik
    0xFF02,   // Pranav
    0xFF03    // Ayushi
};

const String TARGET_LABELS[NUM_TARGETS] = {
    "ff01",
    "ff02",
    "ff03"
};
// ────────────────────────────────────────────────────

struct DeviceState {
    long  rssiSum      = 0;
    int   rssiCount    = 0;
    int   streak       = 0;
    bool  seenThisScan = false;
};

DeviceState states[NUM_TARGETS];
BLEScan* pBLEScan;

// Convert BLEUUID to a comparable 16-bit value
// Returns 0 if not a 16-bit UUID
uint16_t extractUUID16(BLEUUID uuid) {
    return uuid.bitSize() == 16 ? uuid.getNative()->uuid.uuid16 : 0;
}

class MyAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice advertisedDevice) override {
        int rssi = advertisedDevice.getRSSI();
        if (rssi < RSSI_FLOOR) return;

        // Check if this device advertises any of our target service UUIDs
        int svcCount = advertisedDevice.getServiceUUIDCount();
        for (int s = 0; s < svcCount; s++) {
            BLEUUID svcUUID = advertisedDevice.getServiceUUID(s);

            for (int i = 0; i < NUM_TARGETS; i++) {
                // Compare as BLEUUID objects (handles 16-bit ↔ 128-bit matching)
                if (svcUUID.equals(BLEUUID(TARGET_UUIDS[i]))) {
                    states[i].rssiSum += rssi;
                    states[i].rssiCount++;
                    states[i].seenThisScan = true;
                }
            }
        }
    }
};

void sendBLEMessage() {
    StaticJsonDocument<512> doc;
    doc["type"] = "ble";
    doc["ts"]   = millis() / 1000;

    JsonArray devices = doc.createNestedArray("devices");

    for (int i = 0; i < NUM_TARGETS; i++) {
        if (states[i].streak >= CONFIRM_SCANS && states[i].rssiCount > 0) {
            JsonObject dev = devices.createNestedObject();
            dev["uuid"] = TARGET_LABELS[i];
            dev["rssi"] = states[i].rssiSum / states[i].rssiCount;
        }
    }

    // Always send — empty devices array means no one detected (guest)
    serializeJson(doc, Serial);
    Serial.println();
}

void sendTempMessage() {
    float h = dht.readHumidity();
    float t = dht.readTemperature(true); // Reads Celsius by default

    // If the sensor is unplugged or wiring is bad, fail gracefully
    if (isnan(h) || isnan(t)) {
        // Send an error JSON so your Python orchestrator can log hardware failure
        Serial.println("{\"type\":\"error\",\"message\":\"Failed to read from DHT sensor!\"}");
        return;
    }

    StaticJsonDocument<128> doc;
    doc["type"]     = "temp";
    doc["temp_f"]   = t; 
    doc["humidity"] = h;
    doc["ts"]       = millis() / 1000;

    serializeJson(doc, Serial);
    Serial.println();
}

void setup() {
    Serial.begin(115200);
    dht.begin();
    BLEDevice::init("");
    pBLEScan = BLEDevice::getScan();
    pBLEScan->setAdvertisedDeviceCallbacks(new MyAdvertisedDeviceCallbacks(), true);
    pBLEScan->setActiveScan(true);
    pBLEScan->setInterval(100);
    pBLEScan->setWindow(99);
}

int scanCount = 0;

void loop() {
    // Reset per-scan state
    for (int i = 0; i < NUM_TARGETS; i++) {
        states[i].rssiSum      = 0;
        states[i].rssiCount    = 0;
        states[i].seenThisScan = false;
    }

    pBLEScan->start(SCAN_SECONDS, false);
    pBLEScan->clearResults();

    // Update streaks
    for (int i = 0; i < NUM_TARGETS; i++) {
        states[i].streak = states[i].seenThisScan ? states[i].streak + 1 : 0;
    }

    sendBLEMessage();

    // Send temp every 2 scans (~every 10s)
    if (++scanCount % 2 == 0) {
        sendTempMessage();
    }
}
