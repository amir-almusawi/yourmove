#pragma once

// Factory bootstrap config for prebuilt hardware.
// This file should not contain customer Wi-Fi or MQTT credentials.

#define YM_PRODUCT_SLUG "yourmove"
#define YM_PROVISION_URL "https://yourmove.live/api/device/provision"

// MQTT host is stable product infrastructure, not customer-specific.
#define YM_DEFAULT_MQTT_HOST "yourmove.live"
#define YM_DEFAULT_MQTT_PORT 1883

#define YM_RUNTIME_TYPE "esp32_direct"
#define YM_DEVICE_TYPE "water_turret"
#define YM_FIRMWARE_VERSION "esp32-prebuilt-0.1.0"
#define YM_CLIENT_ID_PREFIX "yourmove-esp32"

// Factory serial for this physical unit.
// Leave blank to derive one from the chip id during development.
#define YM_FACTORY_HARDWARE_SERIAL ""

// Setup AP behavior.
#define YM_SETUP_AP_PREFIX "YourMove-Setup"
#define YM_SETUP_AP_PASSWORD ""

// Optional reset button. Set to -1 if not wired yet.
#define YM_RESET_BUTTON_PIN -1
#define YM_RESET_BUTTON_ACTIVE_LOW 1
#define YM_WIFI_RESET_HOLD_MS 5000UL
#define YM_FACTORY_RESET_HOLD_MS 12000UL

// Pinout for the ESP32-C3 Super Mini build.
#define YM_PAN_SERVO_PIN 4
#define YM_TILT_SERVO_PIN 5
#define YM_RELAY_PIN 3

#define YM_RELAY_ACTIVE_HIGH 1

#define YM_SERVO_MIN_US 500
#define YM_SERVO_MAX_US 2400
#define YM_PAN_CENTER_DEG 90.0f
#define YM_TILT_CENTER_DEG 90.0f
#define YM_PAN_INVERT false
#define YM_TILT_INVERT false

#define YM_PAN_MIN -90.0f
#define YM_PAN_MAX 90.0f
#define YM_TILT_MIN -90.0f
#define YM_TILT_MAX 90.0f

#define YM_FIRE_MAX_MS 300000
#define YM_COOLDOWN_MS 2000
#define YM_MOTION_STEP_DEGREES 0.75f
#define YM_MOTION_STEP_INTERVAL_MS 15UL
#define YM_MOTION_TARGET_LEAD_DEGREES 6.0f

#define YM_TELEMETRY_INTERVAL_MS 15000UL
#define YM_HEARTBEAT_INTERVAL_MS 30000UL
#define YM_WIFI_CONNECT_TIMEOUT_MS 20000UL

#define YM_COMMAND_CACHE_SIZE 8
#define YM_REQUIRE_SESSION_ID 0
