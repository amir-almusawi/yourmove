#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

#include "config.h"

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

Servo panServo;
Servo tiltServo;

struct CommandOutcome {
  String id;
  String ackStatus;
  String resultStatus;
  String reason;
};

CommandOutcome recentOutcomes[YM_COMMAND_CACHE_SIZE];
int recentOutcomeIndex = 0;

String activeState = "idle";
String activeSessionId = "";
String lastCompletedCommandId = "";
String lastError = "";
float currentPan = 0.0f;
float currentTilt = 0.0f;
unsigned long lastTelemetryMs = 0;
unsigned long lastHeartbeatMs = 0;
unsigned long lastFireEndedMs = 0;

String topicFor(const char* suffix) {
  return String("nodes/") + YM_NODE_ID + "/" + suffix;
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
  return constrain(value, YM_PAN_MIN, YM_PAN_MAX);
}

float clampTilt(float value) {
  return constrain(value, YM_TILT_MIN, YM_TILT_MAX);
}

int logicalToServoDegrees(float logicalValue, float centerDeg, bool invert) {
  float direction = invert ? -1.0f : 1.0f;
  float servoDeg = centerDeg + (logicalValue * direction);
  return (int)constrain(roundf(servoDeg), 0.0f, 180.0f);
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

void applyServoTargets() {
  panServo.write(logicalToServoDegrees(currentPan, YM_PAN_CENTER_DEG, YM_PAN_INVERT));
  tiltServo.write(logicalToServoDegrees(currentTilt, YM_TILT_CENTER_DEG, YM_TILT_INVERT));
}

void setHomePosition() {
  currentPan = 0.0f;
  currentTilt = 0.0f;
  applyServoTargets();
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
  float nextPan = payload["pan"] | currentPan;
  float nextTilt = payload["tilt"] | currentTilt;

  currentPan = clampPan(nextPan);
  currentTilt = clampTilt(nextTilt);
  applyServoTargets();

  publishAck(commandId, "ack");
  lastCompletedCommandId = commandId;
  lastError = "";
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();
  publishResult(commandId, "completed");
}

void handleModeCommand(const char* commandId, const char* commandType, const char* sessionId) {
  publishAck(commandId, "ack");

  if (strcmp(commandType, "arm") == 0) {
    activeState = "armed";
    activeSessionId = sessionId ? String(sessionId) : "";
  } else if (strcmp(commandType, "disarm") == 0) {
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

  if (millis() - lastFireEndedMs < YM_COOLDOWN_MS) {
    rejectCommand(commandId, "cooldown active");
    return;
  }

  int requestedDuration = payload["duration_ms"] | YM_FIRE_MAX_MS;
  int fireDuration = constrain(requestedDuration, 1, YM_FIRE_MAX_MS);

  publishAck(commandId, "ack");
  activeState = "executing";
  publishState();

  digitalWrite(YM_RELAY_PIN, relayOnState());
  delay(fireDuration);
  safeOutputs();

  activeState = "cooldown";
  lastCompletedCommandId = commandId;
  lastError = "";
  rememberOutcome(commandId, "ack", "completed", nullptr);
  publishState();
  publishResult(commandId, "completed");

  delay(YM_COOLDOWN_MS);
  lastFireEndedMs = millis();
  activeState = "armed";
  publishState();
}

void onCommand(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    lastError = "invalid json";
    return;
  }

  const char* commandId = doc["command_id"];
  const char* commandType = doc["type"];
  const char* sessionId = doc["session_id"] | "";
  long expiresAt = doc["expires_at"] | 0;

  if (!commandId || !commandType) {
    return;
  }

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

  if (strcmp(commandType, "arm") == 0 || strcmp(commandType, "disarm") == 0 || strcmp(commandType, "home") == 0) {
    handleModeCommand(commandId, commandType, sessionId);
    return;
  }

  if (strcmp(commandType, "fire") == 0) {
    handleFireCommand(commandId, commandPayload);
    return;
  }

  rejectCommand(commandId, "unsupported command");
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  safeOutputs();
  activeState = "idle";
  activeSessionId = "";

  WiFi.mode(WIFI_STA);
  WiFi.begin(YM_WIFI_SSID, YM_WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
}

void ensureMqtt() {
  if (mqttClient.connected()) {
    return;
  }

  safeOutputs();
  activeState = "idle";
  activeSessionId = "";

  String clientId = String(YM_CLIENT_ID_PREFIX) + "-" + String((uint32_t) ESP.getEfuseMac(), HEX);
  String lwtPayload = String("{\"state\":\"offline\",\"runtime_type\":\"") + YM_RUNTIME_TYPE + "\",\"reason\":\"disconnect\",\"timestamp\":" + String(monotonicTs()) + "}";

  while (!mqttClient.connected()) {
    bool connected = mqttClient.connect(
      clientId.c_str(),
      YM_MQTT_USERNAME,
      YM_MQTT_PASSWORD,
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
  setHomePosition();
}

void setupRelay() {
  pinMode(YM_RELAY_PIN, OUTPUT);
  safeOutputs();
}

void setup() {
  Serial.begin(115200);

  setupRelay();
  setupServos();

  mqttClient.setServer(YM_MQTT_HOST, YM_MQTT_PORT);
  mqttClient.setBufferSize(1024);
  mqttClient.setCallback(onCommand);

  ensureWifi();
  ensureMqtt();
}

void loop() {
  ensureWifi();
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
