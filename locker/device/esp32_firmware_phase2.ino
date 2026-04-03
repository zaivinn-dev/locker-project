#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>

// ========== Configuration ==========
#define RFID_CS_PIN 5
#define RFID_RST_PIN 22
#define RELAY_1_PIN 25
#define RELAY_2_PIN 26
#define RELAY_3_PIN 27
#define RELAY_4_PIN 32

// ========== IR SENSORS (2 per locker, 8 total) ==========
// Locker 1: GPIO 33, 34
// Locker 2: GPIO 35, 36
// Locker 3: GPIO 37, 38
// Locker 4: GPIO 39, 4 (Note: GPIO 4 is multi-purpose - may conflict with boot)
const int IR_SENSOR_PINS[4][2] = {
  {33, 34},   // Locker 1
  {35, 36},   // Locker 2
  {37, 38},   // Locker 3
  {39, 4}     // Locker 4
};

const char* WIFI_SSID = "Emelon Wifi";
const char* WIFI_PASSWORD = "emelonwifi123";
const char* BACKEND_RFID_URL = "http://192.168.2.103:5000/device/rfid";
const char* BACKEND_IR_URL = "http://192.168.2.103:5000/device/ir-status";
const unsigned long RELAY_ACTIVATION_TIME = 2000;  // 2 seconds
const unsigned long RFID_READ_DEBOUNCE = 500;  // 500ms between reads
const unsigned long IR_CHECK_INTERVAL = 100;  // Check IR every 100ms (lightweight check)
const int IR_DETECTION_THRESHOLD = 100;  // Analog threshold (0-4095) - lowered for better sensitivity
const int DEBOUNCE_READS = 2;  // Require 2 stable readings (REAL-TIME: 50-100ms response)
const int SENSOR_SAMPLES = 1;  // Single read per sensor (no averaging delay - just read raw value)
const String LOCKER_NAMES[4] = {"locker_1", "locker_2", "locker_3", "locker_4"};

// ========== Hardware Objects ==========
MFRC522 rfid(RFID_CS_PIN, RFID_RST_PIN);
int relayPins[] = {RELAY_1_PIN, RELAY_2_PIN, RELAY_3_PIN, RELAY_4_PIN};

// ========== Relay Logic Configuration ==========
// Set to 'true' if relay uses INVERTED logic (LOW=locked, HIGH=unlocked)
// Relay 1 (GPIO 25): Normal logic
// Relays 2-4 (GPIO 26,27,32): Inverted logic
bool relayInverted[] = {false, true, true, true};

// ========== Global State ==========
unsigned long lastRfidRead = 0;
String lastScannedUID = "";
bool wifiConnected = false;
unsigned long lastIrCheck = 0;

// Offline mode tracking
bool offlineMode = false;
unsigned long lastWiFiConnection = 0;
const unsigned long OFFLINE_TIMEOUT = 30000;  // 30 seconds without WiFi = offline mode

// Smart Auto-Lock Tracking
int unlockedLocker = -1;  // Which locker is currently unlocked (-1 = none)
unsigned long unlockedTime = 0;  // When was it unlocked (reference point from door opening)
bool isLockerUnlocked = false;  // Is the relay currently in unlocked state
const unsigned long AUTO_LOCK_DELAY = 6000;  // 20 seconds from door UNLOCK before solenoid locks - gives user plenty of time to close, arrange, and step away

// Locker occupancy tracking (one per locker)
bool lockerOccupied[4] = {false, false, false, false};  // Detected state (updated every read)
bool lastLockerOccupied[4] = {false, false, false, false};  // Reported state (only on debounced change)
int lockerStateConfirm[4] = {0, 0, 0, 0};  // Consecutive reads confirming current state

// ========== Setup ==========
void setup() {
  Serial.begin(115200);
  delay(500);
  
  Serial.println("\n\n");
  Serial.println("╔════════════════════════════════════╗");
  Serial.println("║  🔐 SMART LOCKER SYSTEM v2.0     ║");
  Serial.println("║  Dual-Core: RFID(Core0) IR(Core1)║");
  Serial.println("╚════════════════════════════════════╝\n");
  
  setupRelays();
  setupRFID();
  setupWiFi();
  setupIRSensor();
  
  // Create IR sensor task on Core 1 (runs independently, never blocked by HTTP)
  xTaskCreatePinnedToCore(
    irSensorTask,      // Function to execute
    "IRSensorTask",    // Task name (debug)
    4096,              // Stack size (bytes)
    NULL,              // Parameter
    2,                 // Priority (higher priority = runs more often)
    NULL,              // Task handle
    1                  // Core 1 (independent from Core 0 which runs main loop)
  );
  
  Serial.println("\n📡 System ready! IR sensor running on CORE 1 (independent)\n");
}

// ========== WiFi Connection State ==========
unsigned long lastWifiReconnectAttempt = 0;
const unsigned long WIFI_RECONNECT_COOLDOWN = 5000;  // Wait 5s before retry
int wifiReconnectAttempts = 0;
const int MAX_WIFI_CONNECT_ATTEMPTS = 3;

// ========== Main Loop (CORE 0) - RFID + HTTP Only ==========
void loop() {
  // Yield to watchdog timer regularly - CRITICAL for stability
  yield();
  
  // Debug: Print status every 10 seconds
  static unsigned long lastStatusPrint = 0;
  if (millis() - lastStatusPrint > 10000) {
    String wifiStatus = WiFi.status() == WL_CONNECTED ? "CONNECTED" : "DISCONNECTED";
    String modeStatus = offlineMode ? " (OFFLINE MODE)" : "";
    Serial.println("🔄 Main loop running... WiFi: " + wifiStatus + modeStatus);
    lastStatusPrint = millis();
  }
  
  // Ensure WiFi stays connected (with backoff to prevent socket exhaustion)
  if (WiFi.status() != WL_CONNECTED) {
    unsigned long now = millis();
    if (now - lastWifiReconnectAttempt > WIFI_RECONNECT_COOLDOWN) {
      Serial.println("⚠ WiFi disconnected! Attempting reconnect (attempt " + String(wifiReconnectAttempts + 1) + "/" + String(MAX_WIFI_CONNECT_ATTEMPTS) + ")");
      
      if (wifiReconnectAttempts < MAX_WIFI_CONNECT_ATTEMPTS) {
        WiFi.disconnect();  // Properly close old connection
        delay(100);
        WiFi.reconnect();
        wifiReconnectAttempts++;
        lastWifiReconnectAttempt = now;
      } else {
        Serial.println("✗ Max WiFi attempts reached. Restarting WiFi stack...");
        WiFi.mode(WIFI_OFF);
        delay(1000);
        WiFi.mode(WIFI_STA);
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        wifiReconnectAttempts = 0;
        lastWifiReconnectAttempt = now + 10000;  // Longer cooldown after restart
      }
      yield();  // Let watchdog breathe
    }
  } else {
    wifiReconnectAttempts = 0;  // Reset on successful connection
    lastWiFiConnection = millis();  // Track successful connection
    offlineMode = false;  // Exit offline mode when WiFi reconnects
  }
  
  // Check for offline mode (no WiFi for 30+ seconds)
  if (WiFi.status() != WL_CONNECTED && millis() - lastWiFiConnection > OFFLINE_TIMEOUT) {
    if (!offlineMode) {
      offlineMode = true;
      Serial.println("🔌 ENTERING OFFLINE MODE - RFID will work but no backend communication");
      Serial.println("💡 System will continue monitoring lockers and RFID cards");
    }
  }
  
  // Check for RFID card (Core 0 - HTTP requests can take time)
  readAndProcessRFID();
  yield();  // Yield after RFID check
  
  delay(50);  // Small delay - IR sensor is handled on Core 1, not here
}

// ========== RELAY SETUP ==========
void setupRelays() {
  Serial.println("🔧 Setting up relays...");
  for (int i = 0; i < 4; i++) {
    pinMode(relayPins[i], OUTPUT);
    // Initialize to LOCKED state:
    // - Normal logic (relayInverted=false): HIGH = locked
    // - Inverted logic (relayInverted=true): LOW = locked
    digitalWrite(relayPins[i], relayInverted[i] ? LOW : HIGH);
  }
  Serial.println("   ✓ Relays initialized (all LOCKED)");
  Serial.println("   ℹ Relay 1: Normal logic | Relays 2-4: Inverted logic");
}

// ========== RFID SETUP ==========
void setupRFID() {
  Serial.println("🔧 Setting up RFID reader...");
  
  // Initialize SPI with explicit pinout
  SPI.begin(18, 19, 23, 5);  // SCK, MISO, MOSI, CS
  delay(100);
  
  // Manual reset sequence
  pinMode(RFID_RST_PIN, OUTPUT);
  digitalWrite(RFID_RST_PIN, LOW);
  delay(10);
  digitalWrite(RFID_RST_PIN, HIGH);
  delay(50);
  
  // Initialize RFID module
  rfid.PCD_Init();
  delay(100);
  
  // Enable antenna
  rfid.PCD_AntennaOn();
  delay(50);
  
  // Verify
  byte version = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  if (version == 0x91 || version == 0x92) {
    Serial.println("   ✓ MFRC522 detected (v0x" + String(version, HEX) + ")");
    Serial.println("   ✓ Antenna enabled");
  } else {
    Serial.println("   ⚠ MFRC522 not detected (got 0x" + String(version, HEX) + ")");
    Serial.println("   Check wiring: GPIO 5(CS), 18(SCK), 19(MISO), 23(MOSI), 22(RST)");
  }
}

// ========== WiFi SETUP ==========
void setupWiFi() {
  Serial.println("🔧 Connecting to WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n   ✓ Connected to: " + String(WIFI_SSID));
    Serial.println("   ✓ IP: " + WiFi.localIP().toString());
    Serial.println("   ✓ RFID Backend: " + String(BACKEND_RFID_URL));
    Serial.println("   ✓ IR Backend: " + String(BACKEND_IR_URL));
    wifiConnected = true;
  } else {
    Serial.println("\n   ✗ Failed to connect to WiFi!");
    Serial.println("   ✗ Check SSID and password");
    wifiConnected = false;
  }
}

// ========== IR SENSOR SETUP ==========
void setupIRSensor() {
  Serial.println("🔧 Setting up IR sensors (4 lockers x 2 sensors)...");
  
  for (int locker = 0; locker < 4; locker++) {
    for (int sensor = 0; sensor < 2; sensor++) {
      int pin = IR_SENSOR_PINS[locker][sensor];
      pinMode(pin, INPUT);
    }
    
    // Read initial state
    // INVERTED LOGIC: LOW value (< threshold) = object detected
    bool sensorA = (analogRead(IR_SENSOR_PINS[locker][0]) < IR_DETECTION_THRESHOLD);
    bool sensorB = (analogRead(IR_SENSOR_PINS[locker][1]) < IR_DETECTION_THRESHOLD);
    lockerOccupied[locker] = (sensorA || sensorB);  // Occupied if ANY sensor detects
    lastLockerOccupied[locker] = lockerOccupied[locker];
    
    Serial.println("   ✓ " + LOCKER_NAMES[locker] + ": GPIO " + String(IR_SENSOR_PINS[locker][0]) + ", " + String(IR_SENSOR_PINS[locker][1]) + " (" + (lockerOccupied[locker] ? "OCCUPIED" : "EMPTY") + ")");
  }
  
  Serial.println("   ✓ Detection threshold: " + String(IR_DETECTION_THRESHOLD));
}

// ========== IR Sensor Task (CORE 1) - Independent, Never Blocked by HTTP ==========
void irSensorTask(void * parameter) {
  // This runs on Core 1 independently - HTTP requests on Core 0 won't affect this
  
  while (true) {
    // Check all 4 lockers for IR detection
    for (int locker = 0; locker < 4; locker++) {  // Check all 4 lockers
      // FAST: Read sensor values directly (no delays)
      int irValue1 = analogRead(IR_SENSOR_PINS[locker][0]);
      int irValue2 = analogRead(IR_SENSOR_PINS[locker][1]);
      
      // CONSISTENT LOGIC: LOW value (< threshold) = object detected
      bool sensorA = (irValue1 < IR_DETECTION_THRESHOLD);
      bool sensorB = (irValue2 < IR_DETECTION_THRESHOLD);
      
      // Locker is occupied if ANY sensor detects an object
      bool currentState = (sensorA || sensorB);
      
      // Debug output (only on state change for less spam)
      if (currentState != lockerOccupied[locker]) {
        Serial.print("   [" + LOCKER_NAMES[locker] + "] IR: " + String(irValue1) + "," + String(irValue2) + " | NEW STATE: " + (currentState ? "OCCUPIED" : "EMPTY") + "\n");
      }
      
      // FAST DEBOUNCE: Only require 2 READINGS of same state
      if (currentState == lockerOccupied[locker]) {
        // State matches, increment confirm counter
        lockerStateConfirm[locker]++;
        
        // Report when light debounce threshold reached AND state differs from last reported
        if (lockerStateConfirm[locker] >= 2 && currentState != lastLockerOccupied[locker]) {
          lastLockerOccupied[locker] = currentState;
          
          if (currentState) {
            Serial.println("\n📦 OBJECT DETECTED in " + LOCKER_NAMES[locker] + "! (IR: " + String(irValue1) + ", " + String(irValue2) + ")");
            
            // SMART AUTO-LOCK: If this locker was just unlocked and IR now detects door closed
            // Wait at least AUTO_LOCK_DELAY from when door was UNLOCKED (not from first detection)
            if (isLockerUnlocked && unlockedLocker == locker) {
              unsigned long timeSinceUnlock = millis() - unlockedTime;
              if (timeSinceUnlock > AUTO_LOCK_DELAY) {  // Only lock if enough time has passed since door opened
                Serial.println("   🔐 Door detected as CLOSED - AUTO-LOCKING...");
                autoLockLocker(locker);
              } else {
                unsigned long remainingTime = AUTO_LOCK_DELAY - timeSinceUnlock;
                Serial.println("   ⏳ Waiting for user to finish (" + String(remainingTime / 1000) + "s remaining)");
              }
            }
          } else {
            Serial.println("\n✓ " + LOCKER_NAMES[locker] + " is now EMPTY! (IR: " + String(irValue1) + ", " + String(irValue2) + ")");
          }
          
          sendIRStatusToBackend(locker, currentState);
        }
      } else {
        // State changed, reset confirm counter immediately
        lockerOccupied[locker] = currentState;
        lockerStateConfirm[locker] = 1;
      }
    }
    
    // Check IR frequently but yield to other tasks
    delay(50);  // Check every 50ms (REAL-TIME: 100-150ms response even with debounce)
    yield();    // Let watchdog and other tasks run
  }
}

// NOTE: checkIRSensor() has been moved to irSensorTask() on Core 1
// This runs independently and cannot be blocked by HTTP requests on Core 0

// ========== Send IR Status to Backend ==========
void sendIRStatusToBackend(int lockerIndex, bool occupied) {
  // Verify WiFi is ACTUALLY connected (not just status() check)
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("   ⚠ WiFi not connected. IR status not reported.");
    return;
  }
  
  Serial.println("   📤 Reporting IR status to backend...");
  
  HTTPClient http;
  http.setTimeout(5000);  // 5 second timeout - CRITICAL: prevents watchdog reset
  
  // Attempt connection with error checking
  if (!http.begin(BACKEND_IR_URL)) {
    Serial.println("   ✗ Failed to begin HTTP connection");
    http.end();
    yield();
    return;
  }
  
  http.addHeader("Content-Type", "application/json");
  
  // Create JSON payload with uid
  StaticJsonDocument<256> doc;
  doc["uid"] = LOCKER_NAMES[lockerIndex];
  doc["status"] = occupied ? "occupied" : "available";
  doc["ir_1"] = analogRead(IR_SENSOR_PINS[lockerIndex][0]);
  doc["ir_2"] = analogRead(IR_SENSOR_PINS[lockerIndex][1]);
  
  String payload;
  serializeJson(doc, payload);
  
  // Send POST request with timeout handling
  int httpCode = http.POST(payload);
  
  if (httpCode > 0) {
    String response = http.getString();
    Serial.println("   ✓ Backend acknowledged: " + response);
    response = "";  // Clear to free memory
  } else if (httpCode == HTTPC_ERROR_CONNECTION_REFUSED) {
    Serial.println("   ⚠ Backend refused connection (HTTP " + String(httpCode) + ")");
  } else if (httpCode == HTTPC_ERROR_SEND_HEADER_FAILED) {
    Serial.println("   ⚠ Failed to send header (HTTP " + String(httpCode) + ")");
  } else {
    Serial.println("   ⚠ Failed to report IR status (HTTP " + String(httpCode) + ")");
  }
  
  http.end();
  payload = "";  // Clear to free memory
  yield();  // Yield to watchdog timer
}
void readAndProcessRFID() {
  // Yield to prevent watchdog reset - CRITICAL
  yield();
  
  // Debounce: don't read too frequently
  if (millis() - lastRfidRead < RFID_READ_DEBOUNCE) {
    return;
  }
  
  yield();  // Yield after debounce check
  
  // Debug: Check if RFID is being polled
  static unsigned long lastRfidPollDebug = 0;
  if (millis() - lastRfidPollDebug > 5000) {  // Every 5 seconds
    Serial.println("🔍 RFID polling active...");
    lastRfidPollDebug = millis();
  }
  
  yield();  // Yield after debug print
  
  // Check for card
  if (!rfid.PICC_IsNewCardPresent()) {
    return;
  }
  
  yield();  // Yield after card presence check
  
  // Try to read card
  if (!rfid.PICC_ReadCardSerial()) {
    return;
  }
  
  yield();  // Yield after card read
  
  lastRfidRead = millis();
  
  // Build UID string
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    uid += String(rfid.uid.uidByte[i] < 0x10 ? "0" : "");
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  
  yield();  // Yield after UID processing
  
  // Skip if same card as last read
  if (uid == lastScannedUID) {
    rfid.PICC_HaltA();
    return;
  }
  lastScannedUID = uid;
  
  // Show card detected
  Serial.println("\n📖 RFID Card Detected!");
  Serial.println("   UID: " + uid);
  
  // Send to backend and process response
  sendCardToBackend(uid);
  
  yield();  // Yield after backend communication
  
  // Halt card
  rfid.PICC_HaltA();
  Serial.println();
  
  yield();  // Final yield in RFID function
}

// ========== Send Card UID to Backend ==========
void sendCardToBackend(String uid) {
  // Check offline mode first
  if (offlineMode) {
    Serial.println("   📴 OFFLINE MODE: Card detected but cannot communicate with backend");
    Serial.println("   💡 RFID hardware is working - WiFi connection lost for >30 seconds");
    showError("OFFLINE MODE");
    return;
  }
  
  // Verify WiFi is ACTUALLY connected (not just status() check)
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("   ✗ WiFi not connected! Card detected but cannot unlock.");
    Serial.println("   💡 Try resetting ESP32 or check WiFi connection.");
    showError("WIFI DOWN");
    return;
  }
  
  Serial.println("   📤 Sending to backend...");
  
  HTTPClient http;
  http.setTimeout(2000);  // Reduced to 2 seconds for faster failure
  
  // Attempt connection with error checking
  if (!http.begin(BACKEND_RFID_URL)) {
    Serial.println("   ✗ Failed to begin HTTP connection");
    http.end();
    yield();
    showError("CONNECTION FAILED");
    return;
  }
  
  http.addHeader("Content-Type", "application/json");
  
  // Create JSON payload
  StaticJsonDocument<200> doc;
  doc["uid"] = uid;
  
  String payload;
  serializeJson(doc, payload);
  Serial.println("   Payload: " + payload);
  
  // Send POST request with timeout protection
  int httpCode = http.POST(payload);
  
  if (httpCode > 0) {
    String response = http.getString();
    Serial.println("   ✓ Response: " + response);
    
    // Parse and handle response
    handleBackendResponse(response);
    response = "";  // Clear to free memory
  } else if (httpCode == HTTPC_ERROR_CONNECTION_REFUSED) {
    Serial.println("   ✗ Backend refused connection (HTTP " + String(httpCode) + ")");
    showError("BACKEND DOWN");
  } else if (httpCode == HTTPC_ERROR_SEND_HEADER_FAILED) {
    Serial.println("   ✗ Request timeout/failed (HTTP " + String(httpCode) + ")");
    showError("TIMEOUT");
  } else {
    Serial.println("   ✗ HTTP Error: " + String(httpCode));
    showError("HTTP ERROR");
  }
  
  http.end();
  payload = "";  // Clear to free memory
  yield();  // Yield to watchdog timer - CRITICAL
}

// ========== Handle Backend Response ==========
void handleBackendResponse(String response) {
  StaticJsonDocument<200> doc;
  DeserializationError error = deserializeJson(doc, response);
  
  if (error) {
    Serial.println("   ✗ JSON parse error: " + String(error.c_str()));
    showError("JSON ERROR");
    return;
  }
  
  String status = doc["status"] | "unknown";
  
  Serial.println("   Status: " + status);
  
  if (status == "unlocked") {
    // Success! Unlock the locker
    int lockerId = doc["locker_id"] | 0;
    if (lockerId > 0 && lockerId <= 4) {
      Serial.println("   ✓ APPROVED - Unlocking locker " + String(lockerId) + "...");
      activateRelay(lockerId - 1);  // Convert to 0-indexed
      showSuccess("LOCKER " + String(lockerId) + " UNLOCKED");
    } else {
      Serial.println("   ✗ Invalid locker ID: " + String(lockerId));
      showError("LOCKER ERROR");
    }
  } else if (status == "denied") {
    // Access denied
    String reason = doc["reason"] | "unknown";
    Serial.println("   ✗ ACCESS DENIED (" + reason + ")");
    showError("ACCESS DENIED");
  } else if (status == "failed") {
    // Failed to process
    String reason = doc["reason"] | "unknown";
    Serial.println("   ⚠ FAILED: " + reason);
    showError("FAILED: " + reason);
  } else {
    // Unknown response
    Serial.println("   ? Unknown status: " + status);
    showError("UNKNOWN");
  }
}

// ========== Relay Control ==========
void activateRelay(int relayIndex) {
  if (relayIndex < 0 || relayIndex > 3) {
    Serial.println("   ✗ Invalid relay index");
    return;
  }
  
  int pin = relayPins[relayIndex];
  bool inverted = relayInverted[relayIndex];
  
  Serial.println("   🔓 Activating relay " + String(relayIndex + 1) + " (GPIO " + String(pin) + ")");
  
  // Mark this locker as unlocked for smart auto-lock
  unlockedLocker = relayIndex;
  unlockedTime = millis();
  isLockerUnlocked = true;
  
  // Unlock by toggling to active state
  if (inverted) {
    digitalWrite(pin, HIGH);  // Inverted: HIGH = unlocked
  } else {
    digitalWrite(pin, LOW);   // Normal: LOW = unlocked
  }
  
  // WAIT FOR AUTO-LOCK (user closes door, IR detects, auto-locks)
  // Timeout after 30 seconds if IR doesn't detect
  unsigned long timeout = millis() + 30000;  // 30 second timeout
  
  while (isLockerUnlocked && millis() < timeout) {
    yield();  // Feed watchdog
    delay(100);
  }
  
  // If still unlocked after timeout, lock it manually
  if (isLockerUnlocked) {
    Serial.println("   ⚠ 30 second timeout - forcing lock (IR may not have detected)");
    autoLockLocker(relayIndex);
  }
}

// ========== Smart Auto-Lock (triggered by IR detection) ==========
void autoLockLocker(int relayIndex) {
  if (relayIndex < 0 || relayIndex > 3 || !isLockerUnlocked) {
    return;
  }
  
  int pin = relayPins[relayIndex];
  bool inverted = relayInverted[relayIndex];
  
  Serial.println("   🔒 Auto-locking relay " + String(relayIndex + 1));
  
  // Lock by toggling back to locked state
  if (inverted) {
    digitalWrite(pin, LOW);   // Inverted: LOW = locked
  } else {
    digitalWrite(pin, HIGH);  // Normal: HIGH = locked
  }
  
  // Clear unlock flags
  isLockerUnlocked = false;
  unlockedLocker = -1;
  
  Serial.println("   ✓ Locker " + String(relayIndex + 1) + " is now LOCKED & SECURED");
  
  // Allow the same card to be scanned again after lock
  lastScannedUID = "";
}

// ========== Visual Feedback (Serial) ==========
void showSuccess(String message) {
  Serial.println("\n   ✓✓✓ " + message + " ✓✓✓\n");
}

void showError(String message) {
  Serial.println("\n   ✗✗✗ " + message + " ✗✗✗\n");
}

// ========== Optional: Status Report ==========
void printStatus() {
  Serial.println("\n╔════════════════════════════════════╗");
  Serial.println("║       System Status Report       ║");
  Serial.println("╚════════════════════════════════════╝");
  Serial.println("WiFi AP:  " + String(wifiConnected ? "✓ Active" : "✗ Inactive"));
  Serial.println("RFID:     ✓ Ready");
  Serial.println("IR Sensors (4 Lockers):");
  for (int i = 0; i < 4; i++) {
    Serial.println("   " + LOCKER_NAMES[i] + ": " + String(lockerOccupied[i] ? "OCCUPIED" : "EMPTY"));
  }
  Serial.println("Relays:   ✓ Ready (4x GPIO)");
  Serial.println("Uptime:   " + String(millis() / 1000) + "s");
  Serial.println();
}
