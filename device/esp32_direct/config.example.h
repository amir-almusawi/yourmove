#pragma once

// Copy this file to config.h and fill in the values from the YourMove dashboard.

#define YM_WIFI_SSID "your-wifi"
#define YM_WIFI_PASSWORD "your-password"

// Current live broker path.
// The platform is presently exposing plain MQTT on port 1883.
#define YM_MQTT_HOST "yourmove.live"
#define YM_MQTT_PORT 1883
#define YM_MQTT_USERNAME "node_xxx"
#define YM_MQTT_PASSWORD "replace-me"

// This is the numeric MQTT node id issued by the platform registration flow.
#define YM_NODE_ID 1

#define YM_RUNTIME_TYPE "esp32_direct"
#define YM_DEVICE_TYPE "water_turret"
#define YM_FIRMWARE_VERSION "esp32-c3-pan-tilt-relay-0.1.0"
#define YM_CLIENT_ID_PREFIX "yourmove-esp32"

// Pinout for the ESP32-C3 Super Mini build.
#define YM_PAN_SERVO_PIN 4
#define YM_TILT_SERVO_PIN 5
#define YM_RELAY_PIN 6

// Relay boards vary. Most common modules are active-high.
#define YM_RELAY_ACTIVE_HIGH 1

// Servo pulse range and neutral points.
#define YM_SERVO_MIN_US 500
#define YM_SERVO_MAX_US 2400
#define YM_PAN_CENTER_DEG 90.0f
#define YM_TILT_CENTER_DEG 90.0f
#define YM_PAN_INVERT false
#define YM_TILT_INVERT false

// Logical movement limits reported to the platform and enforced locally.
#define YM_PAN_MIN -70.0f
#define YM_PAN_MAX 70.0f
#define YM_TILT_MIN -20.0f
#define YM_TILT_MAX 35.0f

// Safety limits for the relay-driven action.
#define YM_FIRE_MAX_MS 350
#define YM_COOLDOWN_MS 2000

// Telemetry cadence.
#define YM_TELEMETRY_INTERVAL_MS 15000UL
#define YM_HEARTBEAT_INTERVAL_MS 30000UL

// Keep a short rolling cache so duplicate QoS deliveries do not re-fire.
#define YM_COMMAND_CACHE_SIZE 8

// Turn this on once the platform is always sending session_id.
#define YM_REQUIRE_SESSION_ID 0
