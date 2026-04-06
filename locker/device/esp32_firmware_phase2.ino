#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Adafruit_Fingerprint.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

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
const char* BACKEND_FINGERPRINT_URL = "http://192.168.2.103:5000/device/fingerprint";
const char* BACKEND_FINGERPRINT_ENROLL_URL = "http://192.168.2.103:5000/device/fingerprint/enroll";
const char* BACKEND_FINGERPRINT_START_ENROLL_URL = "http://192.168.2.103:5000/device/fingerprint/start-enrollment";
const char* BACKEND_SCAN_ENABLED_URL = "http://192.168.2.103:5000/api/access/scan-enabled";
const unsigned long FINGERPRINT_POLL_INTERVAL = 100; // Poll fingerprint sensor every 100ms (faster response)
const int FINGERPRINT_RX_PIN = 16;
const int FINGERPRINT_TX_PIN = 17;
const unsigned long RELAY_ACTIVATION_TIME = 2000;  // 2 seconds
const unsigned long RFID_READ_DEBOUNCE = 500;  // 500ms between reads
const unsigned long IR_CHECK_INTERVAL = 100;  // Check IR every 100ms (lightweight check)
const int IR_DETECTION_THRESHOLD = 100;  // Analog threshold (0-4095) - lowered for better sensitivity
const int DEBOUNCE_READS = 2;  // Require 2 stable readings (REAL-TIME: 50-100ms response)
const int SENSOR_SAMPLES = 1;  // Single read per sensor (no averaging delay - just read raw value)
const String LOCKER_NAMES[4] = {"locker_1", "locker_2", "locker_3", "locker_4"};

// ========== Hardware Objects ==========
MFRC522 rfid(RFID_CS_PIN, RFID_RST_PIN);
HardwareSerial fingerSerial(2);
Adafruit_Fingerprint finger(&fingerSerial);
int relayPins[] = {RELAY_1_PIN, RELAY_2_PIN, RELAY_3_PIN, RELAY_4_PIN};

// ========== Relay Logic Configuration ==========
// Set to 'true' if relay uses INVERTED logic (LOW=locked, HIGH=unlocked)
// Relay 1 (GPIO 25): Normal logic
// Relays 2-4 (GPIO 26,27,32): Inverted logic
bool relayInverted[] = {false, true, true, true};

unsigned long lastFingerprintPoll = 0;
unsigned long lastEnrollmentComplete = 0;
const unsigned long ENROLLMENT_COOLDOWN = 3000; // Prevent immediate access scan after enrollment

// Enrollment state
bool enrollmentMode = false;
int enrollmentStep = 0; // 0=waiting, 1=first scan, 2=second scan, 3=enrolled
unsigned long enrollmentStartTime = 0;

// ========== Global State ==========
unsigned long lastRfidRead = 0;
String lastScannedUID = "";
bool wifiConnected = false;
unsigned long lastIrCheck = 0;

// Offline mode tracking
bool offlineMode = false;
unsigned long lastWiFiConnection = 0;
const unsigned long OFFLINE_TIMEOUT = 30000;  // 30 seconds without WiFi = offline mode

// Fingerprint scan permission tracking
bool scanPermissionAllowed = false;
unsigned long lastScanPermissionCheck = 0;
const unsigned long SCAN_PERMISSION_POLL_INTERVAL = 1000;  // Check scan enable state every 1 second

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
  // Disable brownout detector to prevent unwanted resets
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);
  
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
  setupFingerprintSensor();
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
  
  // Check for serial commands
  if (Serial.available()) {
    String raw = Serial.readStringUntil('\n');
    if (raw.length() == 0) {
      raw = Serial.readStringUntil('\r');
    }
    raw.trim();
    raw.replace("\r", "");
    raw.replace("\n", "");

    if (raw.length() > 0) {
      Serial.println("🔧 Serial command received: '" + raw + "'");
      if (raw == "reset_fp") {
        resetFingerprints();
      } else if (raw == "status") {
        printStatus();
      } else {
        Serial.println("❌ Unknown serial command: '" + raw + "'");
      }
    }
  }
  
  // Debug: Print status every 10 seconds
  static unsigned long lastStatusPrint = 0;
  if (millis() - lastStatusPrint > 10000) {
    String wifiStatus = WiFi.status() == WL_CONNECTED ? "CONNECTED" : "DISCONNECTED";
    String modeStatus = offlineMode ? " (OFFLINE MODE)" : "";
    Serial.println("🔄 Main loop running... WiFi: " + wifiStatus + modeStatus + " enrollmentMode: " + String(enrollmentMode));
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

  // Check fingerprint scanner for member access
  readAndProcessFingerprint();
  yield();
  
  // Process fingerprint enrollment if in enrollment mode
  processFingerprintEnrollment();
  yield();
  
  // Check for enrollment commands from backend
  checkForEnrollmentCommand();
  yield();
  
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
    Serial.println("   ✓ Fingerprint Backend: " + String(BACKEND_FINGERPRINT_URL));
    Serial.println("   ✓ Fingerprint Enroll Backend: " + String(BACKEND_FINGERPRINT_ENROLL_URL));
    wifiConnected = true;
  } else {
    Serial.println("\n   ✗ Failed to connect to WiFi!");
    Serial.println("   ✗ Check SSID and password");
    wifiConnected = false;
  }
}

// ========== IR SENSOR SETUP ==========
void setupFingerprintSensor() {
  Serial.println("🔧 Setting up ZA620_M5 fingerprint scanner...");
  fingerSerial.begin(57600, SERIAL_8N1, FINGERPRINT_RX_PIN, FINGERPRINT_TX_PIN);
  delay(100);
  
  finger.begin(57600);
  delay(100);
  
  uint8_t p = finger.verifyPassword();
  if (p == FINGERPRINT_OK) {
    // Optional: Clear all stored fingerprints (uncomment if you want to reset)
    // Serial.println("   🔄 Clearing all stored fingerprint templates...");
    // finger.emptyDatabase();
    // delay(1000);
    // Serial.println("   ✓ All templates cleared. Sensor ready for new enrollments.");
    
    finger.getTemplateCount();
    Serial.println("   ✓ Fingerprint sensor ready. Stored templates: " + String(finger.templateCount));
    
    // Optimize sensor parameters for reliable scanning
    finger.setSecurityLevel(2);  // Security level 2 = good balance between sensitivity and accuracy
    delay(100);
    
    Serial.println("   ✓ Sensor parameters optimized for scanning");
    Serial.println("   💡 Troubleshooting tips:");
    Serial.println("      - If no scans: Check RX(16)/TX(17) connections");
    Serial.println("      - If intermittent: Clean sensor with soft cloth");
    Serial.println("      - If poor quality: Press finger firmly and steadily");
  } else {
    Serial.println("   ✗ Fingerprint sensor not found or not responding.");
    Serial.println("     Check ZA620_M5 wiring: RX->16, TX->17, 3.3V, GND");
    Serial.println("     Also verify sensor is powered and baud rate is 57600");
  }
}

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
      
      // TIMEOUT PROTECTION: If locker has been unlocked for too long without IR detection, force lock
      if (isLockerUnlocked && unlockedLocker == locker) {
        unsigned long timeSinceUnlock = millis() - unlockedTime;
        if (timeSinceUnlock > 30000) {  // 30 second timeout (same as original)
          Serial.println("   ⚠ 30 second timeout - forcing lock (IR may not have detected door closing)");
          autoLockLocker(locker);
        }
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

bool checkScanPermission() {
  // If enrollment is active, don't allow access scanning
  if (enrollmentMode) {
    scanPermissionAllowed = false;
    return false;
  }

  if (millis() - lastScanPermissionCheck < SCAN_PERMISSION_POLL_INTERVAL) {
    return scanPermissionAllowed;
  }

  lastScanPermissionCheck = millis();
  if (offlineMode) {
    scanPermissionAllowed = false;
    return false;
  }

  HTTPClient http;
  http.setTimeout(2000);
  if (!http.begin(BACKEND_SCAN_ENABLED_URL)) {
    http.end();
    scanPermissionAllowed = false;
    return false;
  }

  int httpCode = http.GET();
  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<128> doc;
    DeserializationError error = deserializeJson(doc, response);
    if (!error) {
      scanPermissionAllowed = doc["enabled"] | false;
    } else {
      scanPermissionAllowed = false;
    }
  } else {
    scanPermissionAllowed = false;
  }

  http.end();
  return scanPermissionAllowed;
}

void readAndProcessFingerprint() {
  if (millis() - lastFingerprintPoll < FINGERPRINT_POLL_INTERVAL) {
    return;
  }
  lastFingerprintPoll = millis();

  if (enrollmentMode) {
    return;
  }

  if (millis() - lastEnrollmentComplete < ENROLLMENT_COOLDOWN) {
    return;
  }

  if (!checkScanPermission()) {
    return;
  }

  // Allow fingerprint scanning even when lockers are unlocked
  // Multiple members should be able to access their lockers simultaneously

  uint8_t p = finger.getImage();
  if (p != FINGERPRINT_OK) {
    return;  // No finger detected - normal condition
  }

  p = finger.image2Tz(1);
  if (p != FINGERPRINT_OK) {
    Serial.print("FP CAPTURE ERROR: ");
    Serial.println(p, HEX);
    
    // Provide diagnostic hints based on error code
    if (p == 0x06) {
      Serial.println("  → Poor image quality - place finger more firmly on sensor");
    } else if (p == 0x07) {
      Serial.println("  → Sensor may need cleaning - wipe with soft cloth");
    }
    
    showError("FP CAPTURE ERROR");
    return;
  }

  p = finger.fingerSearch();
  if (p == FINGERPRINT_OK) {
    String uid = String(finger.fingerID);
    Serial.println("🔐 Fingerprint recognized! ID=" + uid);
    sendFingerprintToBackend(uid);
    delay(1000);
  } else if (p == FINGERPRINT_NOTFOUND) {
    Serial.println("  → Fingerprint not in database");
    showError("FP NOT FOUND");
  } else if (p == 0xFE) {
    Serial.println("  → Communication error - sensor may need restart");
    showError("SENSOR ERROR");
  } else {
    Serial.print("  → Search error code: 0x");
    Serial.println(p, HEX);
    showError("FP SEARCH ERROR");
  }
}

void sendFingerprintToBackend(String uid) {
  if (offlineMode || WiFi.status() != WL_CONNECTED) {
    showError("CONNECTION ERROR");
    return;
  }

  HTTPClient http;
  http.setTimeout(3000);

  if (!http.begin(BACKEND_FINGERPRINT_URL)) {
    http.end();
    showError("CONNECTION FAILED");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  StaticJsonDocument<200> doc;
  doc["uid"] = uid;
  String payload;
  serializeJson(doc, payload);

  int httpCode = http.POST(payload);

  if (httpCode > 0) {
    String response = http.getString();
    Serial.println("   HTTP code: " + String(httpCode));
    Serial.println("   Fingerprint backend response raw: " + response);
    handleBackendResponse(response);
  } else {
    Serial.println("   HTTP error code: " + String(httpCode));
    showError("HTTP ERROR");
  }

  http.end();
  payload = "";
}


// ========== Handle Backend Response ==========
void handleBackendResponse(String response) {
  StaticJsonDocument<200> doc;
  DeserializationError error = deserializeJson(doc, response);
  
  if (error) {
    Serial.println("   ✗ JSON parse error: " + String(error.c_str()));
    Serial.println("   Raw response for debug: " + response);
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
  } else if (status == "rejected") {
    // Scanning not enabled (user not on member access page)
    String reason = doc["reason"] | "unknown";
    Serial.println("   ✗ REJECTED (" + reason + ")");
    if (reason == "scanning_not_enabled") {
      showError("USE ACCESS PAGE");
    } else {
      showError("REJECTED");
    }
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
  
  // DON'T BLOCK THE MAIN LOOP - let auto-lock happen asynchronously via IR sensor
  // The IR sensor task on Core 1 will detect when door closes and auto-lock
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

// ========== Fingerprint Enrollment Functions ==========
void startFingerprintEnrollment() {
  if (enrollmentMode) {
    return;
  }

  enrollmentMode = true;
  enrollmentStep = 0;
  enrollmentStartTime = millis();
  Serial.println("🔐 Enrollment mode started");
}

void stopFingerprintEnrollment() {
  if (!enrollmentMode) {
    return;
  }

  enrollmentMode = false;
  enrollmentStep = 0;
  
  // Report enrollment stopped to backend
  HTTPClient http;
  http.setTimeout(3000);
  if (http.begin(BACKEND_FINGERPRINT_START_ENROLL_URL)) {
    http.addHeader("Content-Type", "application/json");
    int httpCode = http.POST("{\"action\":\"stop\"}");
    if (httpCode == 200) {
      Serial.println("✓ Enrollment stop reported to backend");
    }
    http.end();
  }
  
  Serial.println("🔐 Enrollment mode stopped");
}

void processFingerprintEnrollment() {
  if (!enrollmentMode) {
    return;
  }

  // Check for enrollment timeout (2 minutes)
  if (millis() - enrollmentStartTime > 120000) {
    Serial.println("⚠ Enrollment session timed out. Stopping enrollment.");
    stopFingerprintEnrollment();
    // Report timeout error to backend
    return;
  }

  uint8_t p = finger.getImage();
  if (p != FINGERPRINT_OK) {
    return;
  }

  switch (enrollmentStep) {
    case 0: // First scan
      p = finger.image2Tz(1);
      if (p == FINGERPRINT_OK) {
        Serial.println("✓ First scan successful. Remove your finger and place it again for second scan.");
        // Report first scan completion to backend
        bool reportSuccess = sendFingerprintEnrollmentToBackend("temp", 1);
        if (reportSuccess) {
          enrollmentStep = 1;
          unsigned long releaseStart = millis();
          const unsigned long removeTimeout = 15000;
          while (millis() - releaseStart < removeTimeout) {
            if (finger.getImage() != FINGERPRINT_OK) {
              break;
            }
            delay(100);
          }
          if (finger.getImage() == FINGERPRINT_OK) {
            Serial.println("   ⚠ Finger still detected after waiting - please lift your finger completely and try again.");
          }
          delay(200);
        } else {
          Serial.println("✗ Failed to report first scan to backend.");
          stopFingerprintEnrollment();
        }
      } else {
        Serial.println("✗ First scan failed - please try again.");
      }
      break;

    case 1: // Second scan
      Serial.println("⏳ Second scan started - place finger firmly and keep it steady.");
      p = finger.image2Tz(2);
      if (p == FINGERPRINT_OK) {
        Serial.println("   ✓ Second scan captured.");
        p = finger.createModel();
        if (p == FINGERPRINT_OK) {
          finger.getTemplateCount();
          uint16_t templateId = finger.templateCount + 1;
          if (templateId == 0) {
            templateId = 1;
          }

          p = finger.storeModel(templateId);
          if (p == FINGERPRINT_OK) {
            String uid = String(templateId);
            Serial.println("✅ Fingerprint enrolled! Template ID: " + uid);
            bool backendSuccess = sendFingerprintEnrollmentToBackend(uid, 2);
            if (backendSuccess) {
              lastEnrollmentComplete = millis();
              enrollmentStep = 2;
              stopFingerprintEnrollment();
            } else {
              Serial.println("   ⚠ Enrollment reported failed. Please try again.");
              enrollmentStep = 0;
              enrollmentStartTime = millis();
            }
          } else {
            Serial.println("✗ Failed to store template (slot=" + String(templateId) + ").");
            Serial.println("   Please clear existing templates or reboot the device and try again.");
            stopFingerprintEnrollment();
          }
        } else {
          Serial.println("✗ Failed to create model - scans do not match.");
          stopFingerprintEnrollment();
        }
      } else {
        Serial.print("✗ Second scan failed - error code 0x");
        Serial.println(p, HEX);
        if (p == FINGERPRINT_INVALIDIMAGE) {
          Serial.println("   → Invalid image - clean sensor and try again.");
        } else if (p == FINGERPRINT_FEATUREFAIL) {
          Serial.println("   → Poor image quality - press your finger more firmly.");
        } else if (p == FINGERPRINT_PACKETRECIEVEERR) {
          Serial.println("   → Communication error with sensor.");
        }
      }
      break;
  }
}

bool sendFingerprintEnrollmentToBackend(String templateId, int step) {
  if (offlineMode || WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠ Cannot report enrollment: WiFi unavailable.");
    return false;
  }

  HTTPClient http;
  http.setTimeout(5000);

  if (!http.begin(BACKEND_FINGERPRINT_ENROLL_URL)) {
    Serial.println("✗ Failed to open enrollment endpoint.");
    http.end();
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  StaticJsonDocument<200> doc;
  doc["uid"] = templateId;
  doc["step"] = step;  // 1=first scan, 2=enrolled
  String payload;
  serializeJson(doc, payload);

  int httpCode = http.POST(payload);
  Serial.println("   Enrollment POST " + String(httpCode) + " -> " + payload);

  bool success = false;
  if (httpCode > 0) {
    String response = http.getString();
    Serial.println("   Enrollment backend response: " + response);

    StaticJsonDocument<200> respDoc;
    DeserializationError error = deserializeJson(respDoc, response);
    if (error) {
      Serial.println("   ✗ Enrollment response parse error: " + String(error.c_str()));
      showError("ENROLLMENT RESPONSE INVALID");
    } else {
      String status = respDoc["status"] | "";
      if (status == "first_scan_complete" || status == "enrolled") {
        Serial.println("   ✓ Fingerprint enrollment step successfully reported to backend.");
        success = true;
      } else {
        Serial.println("   ✗ Unexpected enrollment response status: " + status);
        showError("ENROLLMENT FAILED");
      }
    }
  } else {
    Serial.println("   Enrollment report failed with HTTP code " + String(httpCode));
    showError("ENROLLMENT REPORT FAILED");
  }

  http.end();
  payload = "";
  return success;
}

void checkForEnrollmentCommand() {
  static unsigned long lastEnrollmentCheck = 0;
  if (millis() - lastEnrollmentCheck < 2000) {
    return;
  }
  lastEnrollmentCheck = millis();

  if (offlineMode || WiFi.status() != WL_CONNECTED) {
    return;
  }

  HTTPClient http;
  http.setTimeout(3000);

  if (!http.begin(BACKEND_FINGERPRINT_START_ENROLL_URL)) {
    http.end();
    return;
  }

  int httpCode = http.POST("{}");

  if (httpCode == 200) {
    String response = http.getString();

    StaticJsonDocument<128> doc;
    DeserializationError error = deserializeJson(doc, response);
    if (!error) {
      const char* status = doc["status"];

      if (status && strcmp(status, "enrollment_started") == 0 && !enrollmentMode) {
        startFingerprintEnrollment();
      } else if (status && strcmp(status, "enrollment_stopped") == 0 && enrollmentMode) {
        stopFingerprintEnrollment();
      }
    }
  }

  http.end();
}

void resetFingerprints() {
  Serial.println("🔄 Resetting fingerprint sensor database...");
  
  uint8_t p = finger.emptyDatabase();
  if (p == FINGERPRINT_OK) {
    Serial.println("✅ All fingerprint templates cleared!");
    Serial.println("📊 Sensor now has 0 stored templates");
    Serial.println("💡 Members need to re-enroll fingerprints");
  } else {
    Serial.println("❌ Failed to clear fingerprint database (error: 0x" + String(p, HEX) + ")");
  }
  
  // Update template count
  finger.getTemplateCount();
  Serial.println("📈 Current template count: " + String(finger.templateCount));
}

void printStatus() {
  Serial.println("📊 System Status:");
  Serial.println("  WiFi: " + String(WiFi.status() == WL_CONNECTED ? "CONNECTED" : "DISCONNECTED"));
  Serial.println("  IP: " + WiFi.localIP().toString());
  Serial.println("  Enrollment Mode: " + String(enrollmentMode ? "ACTIVE" : "INACTIVE"));
  Serial.println("  Offline Mode: " + String(offlineMode ? "YES" : "NO"));
  
  // Fingerprint status
  if (finger.verifyPassword() == FINGERPRINT_OK) {
    finger.getTemplateCount();
    Serial.println("  Fingerprint Templates: " + String(finger.templateCount));
  } else {
    Serial.println("  Fingerprint Sensor: NOT RESPONDING");
  }
  
  Serial.println("📋 Available Commands:");
  Serial.println("  reset_fp  - Clear all fingerprint templates");
  Serial.println("  status    - Show this status information");
}
