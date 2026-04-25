#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <Preferences.h>

#include "config.h"

Preferences prefs;
WebServer portalServer(80);
DNSServer dnsServer;
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

Servo panServo;
Servo tiltServo;

struct StoredConfig {
  String wifiSsid;
  String wifiPassword;
  String hardwareSerial;
  String claimCode;
  String mqttHost;
  uint16_t mqttPort;
  int mqttNodeId;
  String mqttUsername;
  String mqttPassword;
  bool provisioned;
};

struct CommandOutcome {
  String id;
  String ackStatus;
  String resultStatus;
  String reason;
};

StoredConfig cfg;
CommandOutcome recentOutcomes[YM_COMMAND_CACHE_SIZE];
int recentOutcomeIndex = 0;

String activeState = "idle";
String activeSessionId = "";
String lastCompletedCommandId = "";
String lastError = "";
float currentPan = 0.0f;
float currentTilt = 0.0f;
float targetPan = 0.0f;
float targetTilt = 0.0f;
float motionPanRate = 0.0f;
float motionTiltRate = 0.0f;
float runtimePanMin = YM_PAN_MIN;
float runtimePanMax = YM_PAN_MAX;
float runtimeTiltMin = YM_TILT_MIN;
float runtimeTiltMax = YM_TILT_MAX;
unsigned long lastTelemetryMs = 0;
unsigned long lastHeartbeatMs = 0;
unsigned long lastFireEndedMs = 0;
unsigned long lastMotionStepMs = 0;
unsigned long fireEndsAtMs = 0;
unsigned long fireCooldownEndsAtMs = 0;
unsigned long currentFireCooldownMs = YM_COOLDOWN_MS;
unsigned long buttonPressStartedMs = 0;
bool buttonActionTaken = false;
bool fireInProgress = false;
bool fireAllowsMotion = false;
bool setupMode = false;
bool rebootScheduled = false;
unsigned long rebootAtMs = 0;
String setupApSsid = "";
String pendingFireCommandId = "";

String deriveHardwareSerial() {
  if (String(YM_FACTORY_HARDWARE_SERIAL).length() > 0) {
    return String(YM_FACTORY_HARDWARE_SERIAL);
  }

  uint64_t chipId = ESP.getEfuseMac();
  char serial[32];
  snprintf(serial, sizeof(serial), "YM-C3-%06llX", (unsigned long long) (chipId & 0xFFFFFF));
  return String(serial);
}

String htmlEscape(const String& input) {
  String out = input;
  out.replace("&", "&amp;");
  out.replace("<", "&lt;");
  out.replace(">", "&gt;");
  out.replace("\"", "&quot;");
  return out;
}

String topicFor(const char* suffix) {
  return String("nodes/") + String(cfg.mqttNodeId) + "/" + suffix;
}

const char* wifiStatusName(wl_status_t status) {
  switch (status) {
    case WL_IDLE_STATUS:
      return "idle";
    case WL_NO_SSID_AVAIL:
      return "no_ssid";
    case WL_SCAN_COMPLETED:
      return "scan_completed";
    case WL_CONNECTED:
      return "connected";
    case WL_CONNECT_FAILED:
      return "connect_failed";
    case WL_CONNECTION_LOST:
      return "connection_lost";
    case WL_DISCONNECTED:
      return "disconnected";
    default:
      return "unknown";
  }
}

unsigned long monotonicTs() {
  return millis() / 1000;
}

int relayOffState() {
  return YM_RELAY_ACTIVE_HIGH ? LOW : HIGH;
}

int relayOnState() {
  return YM_RELAY_ACTIVE_HIGH ? HIGH : LOW;
}

float clampPan(float value) {
  return constrain(value, runtimePanMin, runtimePanMax);
}

float clampTilt(float value) {
  return constrain(value, runtimeTiltMin, runtimeTiltMax);
}

void resetRuntimeLimits() {
  runtimePanMin = YM_PAN_MIN;
  runtimePanMax = YM_PAN_MAX;
  runtimeTiltMin = YM_TILT_MIN;
  runtimeTiltMax = YM_TILT_MAX;
}

void applyRuntimeLimits(JsonVariant payload) {
  float requestedPanMin = payload["pan_min"].isNull() ? YM_PAN_MIN : float(payload["pan_min"]);
  float requestedPanMax = payload["pan_max"].isNull() ? YM_PAN_MAX : float(payload["pan_max"]);
  float requestedTiltMin = payload["tilt_min"].isNull() ? YM_TILT_MIN : float(payload["tilt_min"]);
  float requestedTiltMax = payload["tilt_max"].isNull() ? YM_TILT_MAX : float(payload["tilt_max"]);

  runtimePanMin = constrain(min(requestedPanMin, requestedPanMax), YM_PAN_MIN, YM_PAN_MAX);
  runtimePanMax = constrain(max(requestedPanMin, requestedPanMax), YM_PAN_MIN, YM_PAN_MAX);
  runtimeTiltMin = constrain(min(requestedTiltMin, requestedTiltMax), YM_TILT_MIN, YM_TILT_MAX);
  runtimeTiltMax = constrain(max(requestedTiltMin, requestedTiltMax), YM_TILT_MIN, YM_TILT_MAX);
}

float logicalToServoDegrees(float logicalValue, float centerDeg, bool invert) {
  float direction = invert ? -1.0f : 1.0f;
  float servoDeg = centerDeg + (logicalValue * direction);
  return constrain(servoDeg, 0.0f, 180.0f);
}

int logicalToServoMicros(float logicalValue, float centerDeg, bool invert) {
  float servoDeg = logicalToServoDegrees(logicalValue, centerDeg, invert);
  float ratio = servoDeg / 180.0f;
  float micros = YM_SERVO_MIN_US + ((YM_SERVO_MAX_US - YM_SERVO_MIN_US) * ratio);
  return (int) lroundf(micros);
}

void scheduleReboot(unsigned long delayMs = 1500UL) {
  rebootScheduled = true;
  rebootAtMs = millis() + delayMs;
}

void loadConfig() {
  prefs.begin("yourmove", true);
  cfg.wifiSsid = prefs.getString("wifi_ssid", "");
  cfg.wifiPassword = prefs.getString("wifi_pw", "");
  cfg.hardwareSerial = prefs.getString("hw_serial", deriveHardwareSerial());
  cfg.claimCode = prefs.getString("claim_code", "");
  cfg.mqttHost = prefs.getString("mqtt_host", YM_DEFAULT_MQTT_HOST);
  cfg.mqttPort = prefs.getUShort("mqtt_port", YM_DEFAULT_MQTT_PORT);
  cfg.mqttNodeId = prefs.getInt("mqtt_node_id", 0);
  cfg.mqttUsername = prefs.getString("mqtt_user", "");
  cfg.mqttPassword = prefs.getString("mqtt_pw", "");
  cfg.provisioned = prefs.getBool("provisioned", false);
  prefs.end();
}

void saveProvisionedConfig(
  const String& wifiSsid,
  const String& wifiPassword,
  const String& claimCode,
  int mqttNodeId,
  const String& mqttUsername,
  const String& mqttPassword,
  const String& mqttHost,
  uint16_t mqttPort
) {
  prefs.begin("yourmove", false);
  prefs.putString("wifi_ssid", wifiSsid);
  prefs.putString("wifi_pw", wifiPassword);
  prefs.putString("hw_serial", cfg.hardwareSerial);
  prefs.putString("claim_code", claimCode);
  prefs.putString("mqtt_host", mqttHost);
  prefs.putUShort("mqtt_port", mqttPort);
  prefs.putInt("mqtt_node_id", mqttNodeId);
  prefs.putString("mqtt_user", mqttUsername);
  prefs.putString("mqtt_pw", mqttPassword);
  prefs.putBool("provisioned", true);
  prefs.end();

  cfg.wifiSsid = wifiSsid;
  cfg.wifiPassword = wifiPassword;
  cfg.claimCode = claimCode;
  cfg.mqttHost = mqttHost;
  cfg.mqttPort = mqttPort;
  cfg.mqttNodeId = mqttNodeId;
  cfg.mqttUsername = mqttUsername;
  cfg.mqttPassword = mqttPassword;
  cfg.provisioned = true;
}

void clearWifiConfig() {
  prefs.begin("yourmove", false);
  prefs.remove("wifi_ssid");
  prefs.remove("wifi_pw");
  prefs.remove("provisioned");
  prefs.remove("mqtt_host");
  prefs.remove("mqtt_port");
  prefs.remove("mqtt_node_id");
  prefs.remove("mqtt_user");
  prefs.remove("mqtt_pw");
  prefs.end();
  loadConfig();
}

void clearAllConfig() {
  prefs.begin("yourmove", false);
  prefs.clear();
  prefs.end();
  loadConfig();
}

void publishJson(const String& topic, JsonDocument& doc, bool retain = false) {
  char buffer[1024];
  size_t len = serializeJson(doc, buffer, sizeof(buffer));
  mqttClient.publish(topic.c_str(), reinterpret_cast<const uint8_t*>(buffer), len, retain);
}

CommandOutcome* findOutcome(const String& commandId) {
  for (int i = 0; i < YM_COMMAND_CACHE_SIZE; i++) {
    if (recentOutcomes[i].id == commandId) {
      return &recentOutcomes[i];
    }
  }
  return nullptr;
}

void rememberOutcome(const String& commandId, const char* ackStatus, const char* resultStatus, const char* reason = nullptr) {
  CommandOutcome& slot = recentOutcomes[recentOutcomeIndex];
  slot.id = commandId;
  slot.ackStatus = ackStatus;
  slot.resultStatus = resultStatus;
  slot.reason = reason ? String(reason) : "";
  recentOutcomeIndex = (recentOutcomeIndex + 1) % YM_COMMAND_CACHE_SIZE;
}

void publishPresence(const char* state, const char* reason = nullptr) {
  if (!cfg.provisioned || cfg.mqttNodeId <= 0 || !mqttClient.connected()) {
    return;
  }
  StaticJsonDocument<256> doc;
  doc["state"] = state;
  doc["runtime_type"] = YM_RUNTIME_TYPE;
  doc["timestamp"] = monotonicTs();
  if (reason) {
    doc["reason"] = reason;
  }
  publishJson(topicFor("presence"), doc, true);
}

void publishCapabilities() {
  StaticJsonDocument<1024> doc;
  doc["node_type"] = YM_DEVICE_TYPE;
  doc["runtime_type"] = YM_RUNTIME_TYPE;
  doc["protocol_version"] = 1;
  doc["firmware_version"] = YM_FIRMWARE_VERSION;

  JsonObject commands = doc.createNestedObject("commands");
  JsonObject setTarget = commands.createNestedObject("set_target");
  setTarget["class"] = "target";
  setTarget["replaceable"] = true;

  JsonObject aim = commands.createNestedObject("aim");
  aim["class"] = "target";
  aim["replaceable"] = true;

  JsonObject setMotion = commands.createNestedObject("set_motion");
  setMotion["class"] = "motion";
  setMotion["replaceable"] = true;

  JsonObject fire = commands.createNestedObject("fire");
  fire["class"] = "action";
  fire["replaceable"] = false;

  JsonObject arm = commands.createNestedObject("arm");
  arm["class"] = "mode";
  arm["replaceable"] = false;

  JsonObject disarm = commands.createNestedObject("disarm");
  disarm["class"] = "mode";
  disarm["replaceable"] = false;

  JsonObject home = commands.createNestedObject("home");
  home["class"] = "mode";
  home["replaceable"] = false;

  JsonObject sweepPan = commands.createNestedObject("sweep_pan");
  sweepPan["class"] = "action";
  sweepPan["replaceable"] = false;

  JsonObject sweepTilt = commands.createNestedObject("sweep_tilt");
  sweepTilt["class"] = "action";
  sweepTilt["replaceable"] = false;

  JsonObject limits = doc.createNestedObject("limits");
  limits["pan_min"] = YM_PAN_MIN;
  limits["pan_max"] = YM_PAN_MAX;
  limits["tilt_min"] = YM_TILT_MIN;
  limits["tilt_max"] = YM_TILT_MAX;
  limits["fire_max_ms"] = YM_FIRE_MAX_MS;
  limits["cooldown_ms"] = YM_COOLDOWN_MS;

  JsonObject pins = doc.createNestedObject("pins");
  pins["pan_gpio"] = YM_PAN_SERVO_PIN;
  pins["tilt_gpio"] = YM_TILT_SERVO_PIN;
  pins["relay_gpio"] = YM_RELAY_PIN;

  publishJson(topicFor("capabilities"), doc, true);
}

void publishState() {
  if (!cfg.provisioned || cfg.mqttNodeId <= 0 || !mqttClient.connected()) {
    return;
  }

  StaticJsonDocument<512> doc;
  doc["state"] = activeState;
  doc["session_id"] = activeSessionId;
  doc["uptime"] = monotonicTs();
  doc["protocol_version"] = 1;
  doc["last_completed_command_id"] = lastCompletedCommandId;
  if (lastError.length() > 0) {
    doc["last_error"] = lastError;
  }

  JsonObject position = doc.createNestedObject("position");
  position["pan"] = currentPan;
  position["tilt"] = currentTilt;

  publishJson(topicFor("state/reported"), doc, true);
}

void publishTelemetry() {
  if (!cfg.provisioned || cfg.mqttNodeId <= 0 || !mqttClient.connected()) {
    return;
  }

  StaticJsonDocument<256> doc;
  doc["uptime"] = monotonicTs();
  doc["free_heap"] = ESP.getFreeHeap();
  doc["rssi"] = WiFi.RSSI();
  if (lastError.length() > 0) {
    doc["last_error"] = lastError;
  }
  publishJson(topicFor("telemetry"), doc, false);
}

void publishHeartbeat() {
  if (!cfg.provisioned || cfg.mqttNodeId <= 0 || !mqttClient.connected()) {
    return;
  }

  StaticJsonDocument<128> doc;
  doc["uptime"] = monotonicTs();
  doc["state"] = activeState;
  publishJson(topicFor("heartbeat"), doc, false);
}

void publishAck(const char* commandId, const char* status, const char* reason = nullptr) {
  StaticJsonDocument<256> doc;
  doc["command_id"] = commandId;
  doc["status"] = status;
  doc["timestamp"] = monotonicTs();
  if (reason) {
    doc["reason"] = reason;
  }
  publishJson(topicFor("ack"), doc, false);
}

void publishResult(const char* commandId, const char* status, const char* reason = nullptr) {
  StaticJsonDocument<256> doc;
  doc["command_id"] = commandId;
  doc["status"] = status;
  doc["timestamp"] = monotonicTs();
  if (reason) {
    doc["reason"] = reason;
  }
  publishJson(topicFor("result"), doc, false);
}

void replayOutcome(const CommandOutcome& outcome) {
  publishAck(outcome.id.c_str(), outcome.ackStatus.c_str(), outcome.reason.length() ? outcome.reason.c_str() : nullptr);
  publishResult(outcome.id.c_str(), outcome.resultStatus.c_str(), outcome.reason.length() ? outcome.reason.c_str() : nullptr);
}

void safeOutputs() {
  digitalWrite(YM_RELAY_PIN, relayOffState());
}

void stopMotion() {
  motionPanRate = 0.0f;
  motionTiltRate = 0.0f;
  resetRuntimeLimits();
}

void processFireState() {
  unsigned long now = millis();

  if (fireInProgress && now >= fireEndsAtMs) {
    safeOutputs();
    fireInProgress = false;
    fireEndsAtMs = 0;
    fireCooldownEndsAtMs = now + currentFireCooldownMs;
    activeState = "cooldown";
    lastCompletedCommandId = pendingFireCommandId;
    lastError = "";
    rememberOutcome(pendingFireCommandId, "ack", "completed", nullptr);
    publishState();
    publishResult(pendingFireCommandId.c_str(), "completed");
    pendingFireCommandId = "";
    fireAllowsMotion = false;
  }

  if (!fireInProgress && fireCooldownEndsAtMs > 0 && now >= fireCooldownEndsAtMs) {
    fireCooldownEndsAtMs = 0;
    lastFireEndedMs = now;
    activeState = "armed";
    publishState();
  }
}

void applyServoTargets() {
  panServo.writeMicroseconds(logicalToServoMicros(currentPan, YM_PAN_CENTER_DEG, YM_PAN_INVERT));
  tiltServo.writeMicroseconds(logicalToServoMicros(currentTilt, YM_TILT_CENTER_DEG, YM_TILT_INVERT));
}

bool positionsCloseEnough(float a, float b) {
  return fabsf(a - b) <= 0.01f;
}

void processMotion() {
  unsigned long now = millis();
  if (now - lastMotionStepMs < YM_MOTION_STEP_INTERVAL_MS) {
    return;
  }
  lastMotionStepMs = now;

  if (!positionsCloseEnough(motionPanRate, 0.0f) || !positionsCloseEnough(motionTiltRate, 0.0f)) {
    float nextPan = clampPan(currentPan + (motionPanRate * YM_MOTION_STEP_DEGREES));
    float nextTilt = clampTilt(currentTilt + (motionTiltRate * YM_MOTION_STEP_DEGREES));
    if (!positionsCloseEnough(nextPan, currentPan) || !positionsCloseEnough(nextTilt, currentTilt)) {
      currentPan = nextPan;
      currentTilt = nextTilt;
      targetPan = currentPan;
      targetTilt = currentTilt;
      applyServoTargets();
    }
    return;
  }

  bool changed = false;

  if (!positionsCloseEnough(currentPan, targetPan)) {
    float deltaPan = targetPan - currentPan;
    float stepPan = constrain(deltaPan, -YM_MOTION_STEP_DEGREES, YM_MOTION_STEP_DEGREES);
    currentPan = clampPan(currentPan + stepPan);
    changed = true;
  }

  if (!positionsCloseEnough(currentTilt, targetTilt)) {
    float deltaTilt = targetTilt - currentTilt;
    float stepTilt = constrain(deltaTilt, -YM_MOTION_STEP_DEGREES, YM_MOTION_STEP_DEGREES);
    currentTilt = clampTilt(currentTilt + stepTilt);
    changed = true;
  }

  if (!changed) {
    return;
  }

  if (fabsf(targetPan - currentPan) < YM_MOTION_STEP_DEGREES) {
    currentPan = targetPan;
  }
  if (fabsf(targetTilt - currentTilt) < YM_MOTION_STEP_DEGREES) {
    currentTilt = targetTilt;
  }

  applyServoTargets();
}

void setHomePosition() {
  stopMotion();
  targetPan = 0.0f;
  targetTilt = 0.0f;
}

void moveTowardBlocking(float goalPan, float goalTilt, float stepDegrees = 1.0f, unsigned long stepDelayMs = 20UL) {
  goalPan = clampPan(goalPan);
  goalTilt = clampTilt(goalTilt);
  while (!positionsCloseEnough(currentPan, goalPan) || !positionsCloseEnough(currentTilt, goalTilt)) {
    if (!positionsCloseEnough(currentPan, goalPan)) {
      float deltaPan = goalPan - currentPan;
      currentPan = clampPan(currentPan + constrain(deltaPan, -stepDegrees, stepDegrees));
      if (fabsf(goalPan - currentPan) < stepDegrees) {
        currentPan = goalPan;
      }
    }
    if (!positionsCloseEnough(currentTilt, goalTilt)) {
      float deltaTilt = goalTilt - currentTilt;
      currentTilt = clampTilt(currentTilt + constrain(deltaTilt, -stepDegrees, stepDegrees));
      if (fabsf(goalTilt - currentTilt) < stepDegrees) {
        currentTilt = goalTilt;
      }
    }
    targetPan = currentPan;
    targetTilt = currentTilt;
    applyServoTargets();
    delay(stepDelayMs);
  }
}

void runSweepTest(const char* axis, JsonVariant payload) {
  stopMotion();
  applyRuntimeLimits(payload);
  activeState = "executing";
  publishState();
  if (strcmp(axis, "pan") == 0) {
    moveTowardBlocking(runtimePanMin, currentTilt);
    moveTowardBlocking(runtimePanMax, currentTilt);
  } else {
    moveTowardBlocking(currentPan, runtimeTiltMin);
    moveTowardBlocking(currentPan, runtimeTiltMax);
  }
  moveTowardBlocking(0.0f, 0.0f);
  activeState = "idle";
  resetRuntimeLimits();
  publishState();
}

void rejectCommand(const char* commandId, const char* reason) {
  lastError = reason;
  rememberOutcome(commandId, "rejected", "rejected", reason);
  publishAck(commandId, "rejected", reason);
  publishResult(commandId, "rejected", reason);
  publishState();
}

bool sessionAllowed(const char* commandType, const char* sessionId) {
  if (strcmp(commandType, "arm") == 0 || strcmp(commandType, "disarm") == 0 || strcmp(commandType, "home") == 0) {
    return true;
  }

  if (YM_REQUIRE_SESSION_ID && (!sessionId || strlen(sessionId) == 0)) {
    return false;
  }

  if (!sessionId || strlen(sessionId) == 0) {
    return true;
  }

  if (activeSessionId.length() == 0) {
    return true;
  }

  return activeSessionId == sessionId;
}

void handleTargetCommand(const char* commandId, JsonVariant payload) {
  stopMotion();
  applyRuntimeLimits(payload);
  bool hasAbsolutePan = !payload["pan"].isNull();
  bool hasAbsoluteTilt = !payload["tilt"].isNull();
  bool hasDeltaPan = !payload["delta_pan"].isNull();
  bool hasDeltaTilt = !payload["delta_tilt"].isNull();
  const char* direction = payload["direction"] | "";

  float nextPan = hasAbsolutePan ? float(payload["pan"]) : currentPan;
  float nextTilt = hasAbsoluteTilt ? float(payload["tilt"]) : currentTilt;

  if (!hasAbsolutePan && hasDeltaPan) {
    nextPan = currentPan + float(payload["delta_pan"]);
  }
  if (!hasAbsoluteTilt && hasDeltaTilt) {
    nextTilt = currentTilt + float(payload["delta_tilt"]);
  }
  if (!hasAbsolutePan && !hasAbsoluteTilt && !hasDeltaPan && !hasDeltaTilt && direction && strlen(direction) > 0) {
    if (strcmp(direction, "left") == 0) {
      nextPan = currentPan - 4.0f;
    } else if (strcmp(direction, "right") == 0) {
      nextPan = currentPan + 4.0f;
    } else if (strcmp(direction, "up") == 0) {
      nextTilt = currentTilt + 4.0f;
    } else if (strcmp(direction, "down") == 0) {
      nextTilt = currentTilt - 4.0f;
    }
  }

  float clampedPan = clampPan(nextPan);
  float clampedTilt = clampTilt(nextTilt);
  float minPanTarget = currentPan - YM_MOTION_TARGET_LEAD_DEGREES;
  float maxPanTarget = currentPan + YM_MOTION_TARGET_LEAD_DEGREES;
  float minTiltTarget = currentTilt - YM_MOTION_TARGET_LEAD_DEGREES;
  float maxTiltTarget = currentTilt + YM_MOTION_TARGET_LEAD_DEGREES;

  targetPan = constrain(clampedPan, minPanTarget, maxPanTarget);
  targetTilt = constrain(clampedTilt, minTiltTarget, maxTiltTarget);
  Serial.printf(
    "[command] target id=%s req_pan=%.2f req_tilt=%.2f cur_pan=%.2f cur_tilt=%.2f target_pan=%.2f target_tilt=%.2f\n",
    commandId,
    nextPan,
    nextTilt,
    currentPan,
    currentTilt,
    targetPan,
    targetTilt
  );

  publishAck(commandId, "ack");
  lastCompletedCommandId = commandId;
  lastError = "";
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();
  publishResult(commandId, "completed");
}

void handleMotionCommand(const char* commandId, JsonVariant payload) {
  applyRuntimeLimits(payload);
  motionPanRate = constrain(float(payload["pan_rate"] | 0.0f), -1.0f, 1.0f);
  motionTiltRate = constrain(float(payload["tilt_rate"] | 0.0f), -1.0f, 1.0f);
  targetPan = currentPan;
  targetTilt = currentTilt;
  Serial.printf(
    "[command] motion id=%s pan_rate=%.2f tilt_rate=%.2f cur_pan=%.2f cur_tilt=%.2f\n",
    commandId,
    motionPanRate,
    motionTiltRate,
    currentPan,
    currentTilt
  );
  publishAck(commandId, "ack");
  lastCompletedCommandId = commandId;
  lastError = "";
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();
  publishResult(commandId, "completed");
}

void handleModeCommand(const char* commandId, const char* commandType, const char* sessionId) {
  Serial.printf("[command] mode id=%s type=%s session=%s\n", commandId, commandType, sessionId ? sessionId : "");
  publishAck(commandId, "ack");

  if (strcmp(commandType, "arm") == 0) {
    activeState = "armed";
    activeSessionId = sessionId ? String(sessionId) : "";
  } else if (strcmp(commandType, "disarm") == 0) {
    stopMotion();
    safeOutputs();
    activeState = "idle";
    activeSessionId = "";
  } else if (strcmp(commandType, "home") == 0) {
    setHomePosition();
  } else {
    rejectCommand(commandId, "unsupported mode command");
    return;
  }

  lastCompletedCommandId = commandId;
  lastError = "";
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();
  publishResult(commandId, "completed");
}

void handleFireCommand(const char* commandId, JsonVariant payload) {
  if (activeState != "armed") {
    rejectCommand(commandId, "node not armed");
    return;
  }

  if (fireInProgress || fireCooldownEndsAtMs > 0 || millis() - lastFireEndedMs < currentFireCooldownMs) {
    rejectCommand(commandId, "cooldown active");
    return;
  }

  int requestedDuration = payload["duration_ms"] | YM_FIRE_MAX_MS;
  int fireDuration = constrain(requestedDuration, 1, YM_FIRE_MAX_MS);
  int requestedCooldown = payload["cooldown_ms"] | YM_COOLDOWN_MS;
  currentFireCooldownMs = constrain(requestedCooldown, 250, 60000);
  bool allowMotionWhileFiring = bool(payload["allow_motion_while_firing"] | false);
  Serial.printf(
    "[command] fire id=%s requested_ms=%d final_ms=%d cooldown_ms=%lu allow_motion=%s state=%s\n",
    commandId,
    requestedDuration,
    fireDuration,
    currentFireCooldownMs,
    allowMotionWhileFiring ? "true" : "false",
    activeState.c_str()
  );

  publishAck(commandId, "ack");
  lastError = "";

  if (allowMotionWhileFiring) {
    activeState = "executing";
    publishState();
    digitalWrite(YM_RELAY_PIN, relayOnState());
    fireInProgress = true;
    fireAllowsMotion = true;
    pendingFireCommandId = String(commandId);
    fireEndsAtMs = millis() + fireDuration;
    fireCooldownEndsAtMs = 0;
    return;
  }

  activeState = "executing";
  publishState();

  digitalWrite(YM_RELAY_PIN, relayOnState());
  delay(fireDuration);
  safeOutputs();

  activeState = "cooldown";
  lastCompletedCommandId = commandId;
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();
  publishResult(commandId, "completed");

  delay(currentFireCooldownMs);
  lastFireEndedMs = millis();
  activeState = "armed";
  publishState();
}

void handleDiagnosticCommand(const char* commandId, const char* commandType, JsonVariant payload) {
  Serial.printf("[command] diagnostic id=%s type=%s\n", commandId, commandType);
  publishAck(commandId, "ack");
  lastCompletedCommandId = commandId;
  lastError = "";
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();

  if (strcmp(commandType, "sweep_pan") == 0) {
    runSweepTest("pan", payload);
  } else if (strcmp(commandType, "sweep_tilt") == 0) {
    runSweepTest("tilt", payload);
  } else {
    rejectCommand(commandId, "unsupported diagnostic command");
    return;
  }

  publishResult(commandId, "completed");
}

void onCommand(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    lastError = "invalid json";
    Serial.println("[command] invalid json");
    return;
  }

  const char* commandId = doc["command_id"];
  const char* commandType = doc["type"];
  const char* sessionId = doc["session_id"] | "";
  long expiresAt = doc["expires_at"] | 0;

  if (!commandId || !commandType) {
    Serial.println("[command] missing command id or type");
    return;
  }
  Serial.printf("[command] recv topic=%s id=%s type=%s\n", topic, commandId, commandType);

  CommandOutcome* existing = findOutcome(commandId);
  if (existing) {
    replayOutcome(*existing);
    return;
  }

  if (expiresAt > 0 && monotonicTs() > (unsigned long) expiresAt) {
    rejectCommand(commandId, "expired");
    return;
  }

  if (!sessionAllowed(commandType, sessionId)) {
    rejectCommand(commandId, "wrong or missing session");
    return;
  }

  JsonVariant commandPayload = doc["payload"];

  if (strcmp(commandType, "aim") == 0 || strcmp(commandType, "set_target") == 0) {
    handleTargetCommand(commandId, commandPayload);
    return;
  }

  if (strcmp(commandType, "set_motion") == 0) {
    handleMotionCommand(commandId, commandPayload);
    return;
  }

  if (strcmp(commandType, "arm") == 0 || strcmp(commandType, "disarm") == 0 || strcmp(commandType, "home") == 0) {
    handleModeCommand(commandId, commandType, sessionId);
    return;
  }

  if (strcmp(commandType, "fire") == 0) {
    handleFireCommand(commandId, commandPayload);
    return;
  }

  if (strcmp(commandType, "sweep_pan") == 0 || strcmp(commandType, "sweep_tilt") == 0) {
    handleDiagnosticCommand(commandId, commandType, commandPayload);
    return;
  }

  rejectCommand(commandId, "unsupported command");
}

bool connectToWifi(const String& ssid, const String& password, unsigned long timeoutMs) {
  WiFi.mode(setupMode ? WIFI_AP_STA : WIFI_STA);
  WiFi.disconnect();
  delay(100);
  Serial.printf("[wifi] connecting to SSID '%s' (len=%u)\n", ssid.c_str(), (unsigned int) ssid.length());
  WiFi.begin(ssid.c_str(), password.c_str());

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    Serial.printf("[wifi] status=%s (%d) elapsed=%lu ms\n", wifiStatusName(WiFi.status()), (int) WiFi.status(), millis() - start);
    delay(250);
  }

  Serial.printf("[wifi] final status=%s (%d)\n", wifiStatusName(WiFi.status()), (int) WiFi.status());
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] connected, local IP=%s gateway=%s\n", WiFi.localIP().toString().c_str(), WiFi.gatewayIP().toString().c_str());
  }
  return WiFi.status() == WL_CONNECTED;
}

String networkOptionsHtml() {
  String options = "";
  WiFi.mode(WIFI_AP_STA);
  WiFi.scanDelete();
  delay(75);
  int count = WiFi.scanNetworks(false, false);
  Serial.printf("[setup] scanNetworks found %d networks\n", count);
  for (int i = 0; i < count; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) {
      continue;
    }
    Serial.printf("[setup] network %d: %s (RSSI %d)\n", i, ssid.c_str(), WiFi.RSSI(i));
    options += "<option value=\"" + htmlEscape(ssid) + "\"></option>";
  }
  WiFi.scanDelete();
  if (options.length() == 0 && cfg.wifiSsid.length() > 0) {
    options += "<option value=\"" + htmlEscape(cfg.wifiSsid) + "\"></option>";
  }
  return options;
}

String networkSelectHtml() {
  String options = "<option value=''>Select a detected network</option>";
  WiFi.mode(WIFI_AP_STA);
  WiFi.scanDelete();
  delay(75);
  int count = WiFi.scanNetworks(false, false);
  Serial.printf("[setup] select scan found %d networks\n", count);
  for (int i = 0; i < count; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) {
      continue;
    }
    options += "<option value=\"" + htmlEscape(ssid) + "\">" + htmlEscape(ssid) + "</option>";
  }
  WiFi.scanDelete();
  return options;
}

String portalHtml(const String& message = "", bool success = false) {
  String color = success ? "#1f7a4c" : "#b00020";
  String html = "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>YourMove Setup</title><style>body{font-family:sans-serif;background:#f4f1ea;margin:0;padding:24px;color:#1d1d1d;}main{max-width:560px;margin:0 auto;background:#fff;padding:24px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.08);}h1{margin-top:0;}label{display:block;margin-top:14px;font-weight:600;}input,select{width:100%;padding:12px;margin-top:6px;border:1px solid #cfc9bd;border-radius:10px;box-sizing:border-box;background:#fff;}button{margin-top:20px;background:#c24d2c;color:#fff;border:0;padding:12px 16px;border-radius:10px;font-weight:700;width:100%;}small{display:block;margin-top:10px;color:#666;} .msg{margin:12px 0;padding:12px;border-radius:10px;background:#f7f3ee;color:";
  html += color;
  html += ";}</style></head><body><main><h1>YourMove Device Setup</h1><p>Hardware serial: <strong>";
  html += htmlEscape(cfg.hardwareSerial);
  html += "</strong></p>";
  if (message.length() > 0) {
    html += "<div class='msg'>" + htmlEscape(message) + "</div>";
  }
  html += "<form method='POST' action='/provision' onsubmit='return syncSsidField();'><label>Detected Wi-Fi Networks</label><select id='ssid-select' name='ssid_select'>";
  html += networkSelectHtml();
  html += "</select><small>Select a detected network, or type a hidden/manual SSID below.</small><label>Hidden or Manual Wi-Fi SSID</label><input id='ssid-input' name='ssid' value='" + htmlEscape(cfg.wifiSsid) + "' placeholder='Only needed for hidden or manual networks'>";
  html += "<small>The SSID will be trimmed automatically. You only need one: the dropdown or the manual field.</small><label>Home Wi-Fi Password</label><input type='password' name='password' value='" + htmlEscape(cfg.wifiPassword) + "' required>";
  html += "<label>Claim Code</label><input name='claim_code' value='" + htmlEscape(cfg.claimCode) + "' placeholder='YM-ABCD1234' required>";
  html += "<button type='submit'>Provision Device</button><small>After success, the device will reboot into live mode.</small></form>";
  html += "<script>function syncSsidField(){var select=document.getElementById('ssid-select');var input=document.getElementById('ssid-input');var selected=select?select.value.trim():'';var typed=input?input.value.trim():'';if(!typed&&selected&&selected!==''&&selected!=='Select a detected network'){input.value=selected;typed=selected;}if(!typed){alert('Choose a Wi-Fi network or type a hidden SSID.');return false;}return true;}</script>";
  html += "</main></body></html>";
  return html;
}

bool callProvisioningApi(
  const String& ssid,
  const String& password,
  const String& claimCode,
  String& errorMessage
) {
  WiFiClientSecure secureClient;
  secureClient.setInsecure();

  HTTPClient http;
  if (!http.begin(secureClient, YM_PROVISION_URL)) {
    errorMessage = "could not open provisioning endpoint";
    return false;
  }

  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<512> requestDoc;
  requestDoc["hardware_serial"] = cfg.hardwareSerial;
  requestDoc["claim_code"] = claimCode;
  requestDoc["firmware_version"] = YM_FIRMWARE_VERSION;
  requestDoc["runtime_type"] = YM_RUNTIME_TYPE;

  String requestBody;
  serializeJson(requestDoc, requestBody);

  int statusCode = http.POST(requestBody);
  String responseBody = http.getString();
  http.end();

  if (statusCode != 200) {
    errorMessage = responseBody.length() ? responseBody : ("http " + String(statusCode));
    return false;
  }

  StaticJsonDocument<1024> responseDoc;
  DeserializationError err = deserializeJson(responseDoc, responseBody);
  if (err) {
    errorMessage = "invalid provisioning response";
    return false;
  }

  int mqttNodeId = responseDoc["platform_node_id"] | 0;
  const char* mqttUsername = responseDoc["mqtt_username"] | "";
  const char* mqttPassword = responseDoc["mqtt_password"] | "";
  if (!mqttNodeId || strlen(mqttUsername) == 0 || strlen(mqttPassword) == 0) {
    errorMessage = "provisioning response missing mqtt credentials";
    return false;
  }

  saveProvisionedConfig(
    ssid,
    password,
    claimCode,
    mqttNodeId,
    String(mqttUsername),
    String(mqttPassword),
    YM_DEFAULT_MQTT_HOST,
    YM_DEFAULT_MQTT_PORT
  );
  return true;
}

void handlePortalRoot() {
  portalServer.send(200, "text/html", portalHtml());
}

void handlePortalProvision() {
  String ssid = portalServer.arg("ssid");
  String ssidSelect = portalServer.arg("ssid_select");
  String password = portalServer.arg("password");
  String claimCode = portalServer.arg("claim_code");
  if (ssid.length() == 0 && ssidSelect.length() > 0) {
    ssid = ssidSelect;
  }
  ssid.trim();
  password.trim();
  claimCode.trim();
  Serial.printf("[setup] provision request: ssid='%s' claim='%s'\n", ssid.c_str(), claimCode.c_str());

  if (ssid.length() == 0 || password.length() == 0 || claimCode.length() == 0) {
    portalServer.send(400, "text/html", portalHtml("Wi-Fi SSID, password, and claim code are required."));
    return;
  }

  if (!connectToWifi(ssid, password, YM_WIFI_CONNECT_TIMEOUT_MS)) {
    WiFi.disconnect(true, false);
    delay(150);
    portalServer.send(400, "text/html", portalHtml("Could not connect to that Wi-Fi network. Check the password and try again."));
    return;
  }

  String errorMessage;
  if (!callProvisioningApi(ssid, password, claimCode, errorMessage)) {
    Serial.printf("[setup] provisioning API failed: %s\n", errorMessage.c_str());
    portalServer.send(400, "text/html", portalHtml("Provisioning failed: " + errorMessage));
    return;
  }

  Serial.println("[setup] provisioning succeeded, rebooting");
  portalServer.send(200, "text/html", portalHtml("Provisioning succeeded. Rebooting into live mode.", true));
  scheduleReboot(2000UL);
}

void startSetupPortal() {
  setupMode = true;
  stopMotion();
  safeOutputs();
  mqttClient.disconnect();
  activeState = "setup_mode";
  activeSessionId = "";

  String suffix = cfg.hardwareSerial;
  suffix.replace(" ", "");
  if (suffix.length() > 4) {
    suffix = suffix.substring(suffix.length() - 4);
  }
  setupApSsid = String(YM_SETUP_AP_PREFIX) + "-" + suffix;
  Serial.printf("[setup] starting setup portal SSID '%s'\n", setupApSsid.c_str());

  WiFi.disconnect(true, true);
  delay(250);
  WiFi.mode(WIFI_AP_STA);
  if (String(YM_SETUP_AP_PASSWORD).length() > 0) {
    WiFi.softAP(setupApSsid.c_str(), YM_SETUP_AP_PASSWORD);
  } else {
    WiFi.softAP(setupApSsid.c_str());
  }

  dnsServer.start(53, "*", WiFi.softAPIP());
  portalServer.on("/", HTTP_GET, handlePortalRoot);
  portalServer.on("/provision", HTTP_POST, handlePortalProvision);
  portalServer.onNotFound(handlePortalRoot);
  portalServer.begin();
}

void ensureRuntimeWifi() {
  if (!cfg.provisioned || WiFi.status() == WL_CONNECTED) {
    return;
  }

  stopMotion();
  safeOutputs();
  activeState = "idle";
  activeSessionId = "";
  connectToWifi(cfg.wifiSsid, cfg.wifiPassword, YM_WIFI_CONNECT_TIMEOUT_MS);
}

void ensureMqtt() {
  if (setupMode || !cfg.provisioned || cfg.mqttNodeId <= 0 || mqttClient.connected()) {
    return;
  }

  stopMotion();
  safeOutputs();
  activeState = "idle";
  activeSessionId = "";

  String clientId = String(YM_CLIENT_ID_PREFIX) + "-" + cfg.hardwareSerial;
  String lwtPayload = String("{\"state\":\"offline\",\"runtime_type\":\"") + YM_RUNTIME_TYPE + "\",\"reason\":\"disconnect\",\"timestamp\":" + String(monotonicTs()) + "}";

  while (WiFi.status() == WL_CONNECTED && !mqttClient.connected()) {
    bool connected = mqttClient.connect(
      clientId.c_str(),
      cfg.mqttUsername.c_str(),
      cfg.mqttPassword.c_str(),
      topicFor("presence").c_str(),
      1,
      true,
      lwtPayload.c_str()
    );

    if (connected) {
      mqttClient.subscribe(topicFor("commands").c_str(), 1);
      publishPresence("online");
      publishCapabilities();
      publishState();
      publishTelemetry();
    } else {
      delay(1000);
    }
  }
}

void setupServos() {
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  panServo.setPeriodHertz(50);
  tiltServo.setPeriodHertz(50);
  panServo.attach(YM_PAN_SERVO_PIN, YM_SERVO_MIN_US, YM_SERVO_MAX_US);
  tiltServo.attach(YM_TILT_SERVO_PIN, YM_SERVO_MIN_US, YM_SERVO_MAX_US);
  currentPan = 0.0f;
  currentTilt = 0.0f;
  targetPan = 0.0f;
  targetTilt = 0.0f;
  applyServoTargets();
  setHomePosition();
}

void setupRelay() {
  pinMode(YM_RELAY_PIN, OUTPUT);
  safeOutputs();
}

bool resetButtonPressed() {
  if (YM_RESET_BUTTON_PIN < 0) {
    return false;
  }
  int level = digitalRead(YM_RESET_BUTTON_PIN);
  return YM_RESET_BUTTON_ACTIVE_LOW ? (level == LOW) : (level == HIGH);
}

void handleResetButton() {
  if (YM_RESET_BUTTON_PIN < 0) {
    return;
  }

  bool pressed = resetButtonPressed();
  unsigned long now = millis();

  if (pressed) {
    if (buttonPressStartedMs == 0) {
      buttonPressStartedMs = now;
      buttonActionTaken = false;
    }

    unsigned long heldMs = now - buttonPressStartedMs;
    if (!buttonActionTaken && heldMs >= YM_FACTORY_RESET_HOLD_MS) {
      clearAllConfig();
      buttonActionTaken = true;
      scheduleReboot();
    }
    return;
  }

  if (buttonPressStartedMs > 0 && !buttonActionTaken) {
    unsigned long heldMs = now - buttonPressStartedMs;
    if (heldMs >= YM_WIFI_RESET_HOLD_MS) {
      clearWifiConfig();
      scheduleReboot();
    }
  }

  buttonPressStartedMs = 0;
  buttonActionTaken = false;
}

void setup() {
  Serial.begin(115200);
  loadConfig();

  if (cfg.hardwareSerial.length() == 0) {
    cfg.hardwareSerial = deriveHardwareSerial();
  }

  if (YM_RESET_BUTTON_PIN >= 0) {
    pinMode(YM_RESET_BUTTON_PIN, YM_RESET_BUTTON_ACTIVE_LOW ? INPUT_PULLUP : INPUT);
  }

  setupRelay();
  setupServos();

  mqttClient.setBufferSize(1024);
  mqttClient.setCallback(onCommand);

  if (!cfg.provisioned || cfg.wifiSsid.length() == 0 || cfg.mqttUsername.length() == 0 || cfg.mqttPassword.length() == 0 || cfg.mqttNodeId <= 0) {
    startSetupPortal();
    return;
  }

  mqttClient.setServer(cfg.mqttHost.c_str(), cfg.mqttPort);
  ensureRuntimeWifi();
  ensureMqtt();
}

void loop() {
  handleResetButton();

  if (rebootScheduled && millis() >= rebootAtMs) {
    ESP.restart();
  }

  if (setupMode) {
    dnsServer.processNextRequest();
    portalServer.handleClient();
    return;
  }

  processFireState();
  processMotion();
  ensureRuntimeWifi();

  if (cfg.mqttHost.length() > 0) {
    mqttClient.setServer(cfg.mqttHost.c_str(), cfg.mqttPort);
  }

  ensureMqtt();
  mqttClient.loop();

  unsigned long now = millis();
  if (now - lastTelemetryMs >= YM_TELEMETRY_INTERVAL_MS) {
    lastTelemetryMs = now;
    publishTelemetry();
  }
  if (now - lastHeartbeatMs >= YM_HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    publishHeartbeat();
    publishPresence("online");
    publishState();
  }
}
