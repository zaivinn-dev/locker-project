#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
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

// ========== IR SENSORS (guest lockers 3 and 4 only, 4 total) ==========
// Locker 1: no IR sensors
// Locker 2: no IR sensors
// Locker 3: GPIO 37, 38
// Locker 4: GPIO 39, 21 (Changed GPIO 4 to 21 - GPIO 4 conflicts with ADC2/WiFi)
const int IR_SENSOR_PINS[4][2] = {
  {-1, -1},   // Locker 1 (no IR)
  {-1, -1},   // Locker 2 (no IR)
  {33, 34},   // Locker 3
  {35, 39}    // Locker 4 - GPIO 39 (VN) is safe for ADC1 (GPIO 21 has no ADC!)
};

const char* WIFI_SSID = "GlobeAtHome_A6350";
const char* WIFI_PASSWORD = "Admin123";
const char* BACKEND_HOST = "http://192.168.254.104:5000";
const char* BACKEND_RFID_URL = "http://192.168.254.104:5000/device/rfid";
const char* BACKEND_IR_URL = "http://192.168.254.104:5000/device/ir-status";
const char* BACKEND_FINGERPRINT_URL = "http://192.168.254.104:5000/device/fingerprint";
const char* BACKEND_FINGERPRINT_ENROLL_URL = "http://192.168.254.104:5000/device/fingerprint/enroll";
const char* BACKEND_FINGERPRINT_START_ENROLL_URL = "http://192.168.254.104:5000/device/fingerprint/start-enrollment";
const char* BACKEND_SCAN_ENABLED_URL = "http://192.168.254.104:5000/api/access/scan-enabled";
const unsigned long FINGERPRINT_POLL_INTERVAL = 100; // Poll fingerprint sensor every 100ms (faster response)
const int FINGERPRINT_RX_PIN = 16;
const int FINGERPRINT_TX_PIN = 17;
const unsigned long RELAY_ACTIVATION_TIME = 2000;  // 2 seconds
const unsigned long RFID_READ_DEBOUNCE = 100;  // 100ms between reads for faster card response

String getHttpClientErrorName(int code) {
  switch (code) {
    case -1: return "CONNECTION_REFUSED";
    case -2: return "SEND_PAYLOAD_FAILED";
    case -3: return "NOT_CONNECTED";
    case -4: return "CONNECTION_LOST";
    case -5: return "NO_STREAM";
    case -6: return "NO_HTTP_SERVER";
    case -7: return "TOO_LESS_RAM";
    case -8: return "ENCODING";
    case -9: return "STREAM_WRITE";
    case -10: return "READ_TIMEOUT";
    case -11: return "READ_STREAM";
    default: return String(code);
  }
}

bool beginBackendRequest(HTTPClient &http, const char *url, int timeoutMs) {
  http.setReuse(true);
  http.setTimeout(timeoutMs);
  if (!http.begin(url)) {
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Accept", "application/json");
  return true;
}

int postJsonPayload(const char *url, const String &payload, String &response, int timeoutMs = 2000) {
  HTTPClient http;
  int httpCode = -1;

  for (int attempt = 1; attempt <= 2; attempt++) {
    if (!beginBackendRequest(http, url, timeoutMs)) {
      Serial.println("   ✗ Failed to begin HTTP connection to backend (attempt " + String(attempt) + ")");
      http.end();
      return -1;
    }
    http.addHeader("Content-Length", String(payload.length()));

    httpCode = http.POST((uint8_t*)payload.c_str(), payload.length());
    if (httpCode > 0) {
      response = http.getString();
      http.end();
      return httpCode;
    }

    String errorName = getHttpClientErrorName(httpCode);
    Serial.println("   ✗ HTTP POST attempt " + String(attempt) + " failed (" + errorName + ")");
    http.end();

    if (attempt == 2) {
      break;
    }

    // Only retry on connection-level failures where the request likely never reached the backend.
    if (httpCode == -1 || httpCode == -4) {
      delay(100);
      continue;
    }

    // Do not retry on READ_STREAM or READ_TIMEOUT. The backend may have processed the request,
    // but the response stream failed before the client could read it.
    break;
  }

  return httpCode;
}

// ========== IR SENSOR OPTIMIZATION (Ultra-fast, High-Sensitivity) ==========
const unsigned long IR_POLL_INTERVAL = 30;  // Poll EVERY 30ms (< 50ms ultra-fast response!)
const int IR_DETECTION_THRESHOLD = 500;  // Analog threshold (0-4095) - HIGH for very sensitive detection
const int DEBOUNCE_READS = 3;  // Require 3 stable readings for accuracy (90ms = still fast)
const int SENSOR_SAMPLES = 2;  // Read each sensor twice for noise filtering
const float IR_SMOOTHING_FACTOR = 0.7;  // Exponential moving average (70% new, 30% old) - fast response
const int IR_HYSTERESIS = 50;  // Hysteresis gap to prevent oscillation (e.g., 500->450 threshold gap)
const String LOCKER_NAMES[4] = {"locker_1", "locker_2", "locker_3", "locker_4"};

// ========== Hardware Objects ==========
MFRC522 rfid(RFID_CS_PIN, RFID_RST_PIN);
HardwareSerial fingerSerial(2);
Adafruit_Fingerprint finger(&fingerSerial);
WebServer webServer(80);
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
const unsigned long SCAN_PERMISSION_POLL_INTERVAL = 200;  // Check scan enable state every 0.2 seconds

// Guest RFID tap-to-toggle tracking
String currentRfidUID = "";             // UID currently being processed by backend
String lastUnlockedGuestUID = "";       // Last guest UID that opened a locker
int lastUnlockedGuestLockerIndex = -1;    // Locker index opened by the last guest UID
bool guestTapUnlock[4] = {false, false, false, false};  // Guest tap-to-toggle session state

// Locker unlocked tracking
bool lockerUnlocked[4] = {false, false, false, false};  // Relay unlocked state per locker

// Locker occupancy tracking (one per locker)
bool lockerOccupied[4] = {false, false, false, false};  // Detected state (updated every read)
bool lastLockerOccupied[4] = {false, false, false, false};  // Reported state (only on debounced change)
int lockerStateConfirm[4] = {0, 0, 0, 0};  // Consecutive reads confirming current state

// IR Sensor Smoothing (Exponential Moving Average for noise filtering)
float irSmoothed[4][2] = {  // Smoothed values for each of 8 sensors
  {2048, 2048},  // Locker 1 (2 sensors)
  {2048, 2048},  // Locker 2 (2 sensors)
  {2048, 2048},  // Locker 3 (2 sensors)
  {2048, 2048}   // Locker 4 (2 sensors)
};
int irDetectionThreshold[4][2] = {  // Per-sensor thresholds (with hysteresis)
  {IR_DETECTION_THRESHOLD, IR_DETECTION_THRESHOLD},
  {IR_DETECTION_THRESHOLD, IR_DETECTION_THRESHOLD},
  {IR_DETECTION_THRESHOLD, IR_DETECTION_THRESHOLD},
  {IR_DETECTION_THRESHOLD, IR_DETECTION_THRESHOLD}
};

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
  setupWebServer();
  
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
  
  // Handle incoming HTTP requests from backend for manual lock/unlock/status
  webServer.handleClient();
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
  Serial.println("🔧 Setting up IR sensors for guest lockers 3 and 4 only...");
  
  for (int locker = 2; locker < 4; locker++) {
    for (int sensor = 0; sensor < 2; sensor++) {
      int pin = IR_SENSOR_PINS[locker][sensor];
      pinMode(pin, INPUT);
    }
    
    // Read initial state for guest lockers only
    // INVERTED LOGIC: LOW value (< threshold) = object detected
    bool sensorA = (analogRead(IR_SENSOR_PINS[locker][0]) < IR_DETECTION_THRESHOLD);
    bool sensorB = (analogRead(IR_SENSOR_PINS[locker][1]) < IR_DETECTION_THRESHOLD);
    lockerOccupied[locker] = (sensorA || sensorB);  // Occupied if ANY sensor detects
    lastLockerOccupied[locker] = lockerOccupied[locker];
    
    Serial.println("   ✓ " + LOCKER_NAMES[locker] + ": GPIO " + String(IR_SENSOR_PINS[locker][0]) + ", " + String(IR_SENSOR_PINS[locker][1]) + " (" + (lockerOccupied[locker] ? "OCCUPIED" : "EMPTY") + ")");
  }
  
  Serial.println("   ✓ Detection threshold: " + String(IR_DETECTION_THRESHOLD));
}

// ========== IR Sensor Task (CORE 1) - OPTIMIZED for Ultra-Fast & Accurate Detection ==========
void irSensorTask(void * parameter) {
  // This runs on Core 1 independently - HTTP requests on Core 0 won't affect this
  // OPTIMIZATIONS:
  // 1. Exponential Moving Average: 70% new reading + 30% old (smooth noise, fast response)
  // 2. Hysteresis: prevents oscillation near threshold (500 vs 550)
  // 3. Multi-level debouncing: 3 readings required (90ms = still ultra-fast)
  // 4. Ultra-fast polling: 30ms interval (< 50ms response target)
  
  unsigned long lastDebugPrint = 0;
  const unsigned long DEBUG_PRINT_INTERVAL = 3000; // Print IR status every 3 seconds
  
  while (true) {
    unsigned long loopStartTime = millis();
    
    // ========== CHECK GUEST LOCKERS ONLY (Locker 3 and 4) ==========
    for (int locker = 2; locker < 4; locker++) {
      
      // ========== SENSOR A (First sensor) ==========
      {
        // Read sensor multiple times for noise filtering
        float rawSum = 0;
        for (int sample = 0; sample < SENSOR_SAMPLES; sample++) {
          rawSum += analogRead(IR_SENSOR_PINS[locker][0]);
        }
        float rawValue = rawSum / SENSOR_SAMPLES;
        
        // Apply exponential moving average (smoothing): 70% new + 30% old
        irSmoothed[locker][0] = (IR_SMOOTHING_FACTOR * rawValue) + ((1.0 - IR_SMOOTHING_FACTOR) * irSmoothed[locker][0]);
        
        // Hysteresis logic: prevent rapid switching near threshold
        int thisThreshold = irDetectionThreshold[locker][0];
        if (irSmoothed[locker][0] < (thisThreshold - IR_HYSTERESIS)) {
          irDetectionThreshold[locker][0] = thisThreshold + IR_HYSTERESIS;  // Raise threshold to prevent re-detection
        } else if (irSmoothed[locker][0] > (thisThreshold + IR_HYSTERESIS)) {
          irDetectionThreshold[locker][0] = thisThreshold - IR_HYSTERESIS;  // Lower threshold
        }
      }
      
      // ========== SENSOR B (Second sensor) ==========
      {
        // Read sensor multiple times for noise filtering
        float rawSum = 0;
        for (int sample = 0; sample < SENSOR_SAMPLES; sample++) {
          rawSum += analogRead(IR_SENSOR_PINS[locker][1]);
        }
        float rawValue = rawSum / SENSOR_SAMPLES;
        
        // Apply exponential moving average (smoothing)
        irSmoothed[locker][1] = (IR_SMOOTHING_FACTOR * rawValue) + ((1.0 - IR_SMOOTHING_FACTOR) * irSmoothed[locker][1]);
        
        // Hysteresis logic
        int thisThreshold = irDetectionThreshold[locker][1];
        if (irSmoothed[locker][1] < (thisThreshold - IR_HYSTERESIS)) {
          irDetectionThreshold[locker][1] = thisThreshold + IR_HYSTERESIS;
        } else if (irSmoothed[locker][1] > (thisThreshold + IR_HYSTERESIS)) {
          irDetectionThreshold[locker][1] = thisThreshold - IR_HYSTERESIS;
        }
      }
      
      // ========== DETERMINE LOCKER STATE ==========
      bool sensorA = (irSmoothed[locker][0] < IR_DETECTION_THRESHOLD);
      bool sensorB = (irSmoothed[locker][1] < IR_DETECTION_THRESHOLD);
      bool currentState = (sensorA || sensorB);  // Occupied if ANY sensor detects
      
      // ========== SMART DEBOUNCING WITH STATE CONFIRMATION ==========
      if (currentState == lockerOccupied[locker]) {
        // State matches last confirmed state, increment confidence
        lockerStateConfirm[locker]++;
        
        // When we have 3 confirmations (90ms at 30ms intervals = ultra-fast)
        // AND it differs from the last reported state
        if (lockerStateConfirm[locker] >= DEBOUNCE_READS && currentState != lastLockerOccupied[locker]) {
          lastLockerOccupied[locker] = currentState;
          
          // State change detected!
          if (currentState) {
            Serial.println("\n⚡ OCCUPANCY CHANGE: " + LOCKER_NAMES[locker] + " → OCCUPIED (IR: " + String((int)irSmoothed[locker][0]) + ", " + String((int)irSmoothed[locker][1]) + ")");
          } else {
            Serial.println("\n⚡ OCCUPANCY CHANGE: " + LOCKER_NAMES[locker] + " → EMPTY (IR: " + String((int)irSmoothed[locker][0]) + ", " + String((int)irSmoothed[locker][1]) + ")");
          }
          
          sendIRStatusToBackend(locker, currentState);
        }
      } else {
        // State changed! Start new debounce sequence
        lockerOccupied[locker] = currentState;
        lockerStateConfirm[locker] = 1;
      }
      
    }
    
    // ========== PERIODIC DEBUG OUTPUT (Every 3 seconds) ==========
    if (millis() - lastDebugPrint > DEBUG_PRINT_INTERVAL) {
      Serial.println("\n🔍 IR SENSOR STATUS (Smoothed Values):");
      for (int locker = 0; locker < 4; locker++) {
        Serial.print("  " + LOCKER_NAMES[locker] + ": [");
        if (locker < 2) {
          Serial.print("N/A, N/A");
          Serial.print("] | State: N/A | Confirm: 0");
        } else {
          Serial.print(String((int)irSmoothed[locker][0]));
          Serial.print(", ");
          Serial.print(String((int)irSmoothed[locker][1]));
          Serial.print("] | State: " + String(lastLockerOccupied[locker] ? "OCCUPIED" : "EMPTY") + " | Confirm: " + String(lockerStateConfirm[locker]));
        }
        Serial.println();
      }
      lastDebugPrint = millis();
    }
    
    // ========== ULTRA-FAST POLLING: 30ms (< 50ms response target!) ==========
    unsigned long loopDuration = millis() - loopStartTime;
    if (loopDuration < IR_POLL_INTERVAL) {
      delay(IR_POLL_INTERVAL - loopDuration);
    }
    yield();  // Let watchdog and other tasks run
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
  if (!beginBackendRequest(http, BACKEND_IR_URL, 1000)) {
    Serial.println("   ✗ Failed to begin HTTP connection for IR status");
    http.end();
    yield();
    return;
  }

  StaticJsonDocument<256> doc;
  doc["uid"] = LOCKER_NAMES[lockerIndex];
  doc["status"] = occupied ? "occupied" : "available";
  doc["ir_1"] = analogRead(IR_SENSOR_PINS[lockerIndex][0]);
  doc["ir_2"] = analogRead(IR_SENSOR_PINS[lockerIndex][1]);
  
  String payload;
  serializeJson(doc, payload);

  int httpCode = http.POST(payload);
  if (httpCode > 0) {
    String response = http.getString();
    Serial.println("   ✓ Backend acknowledged: " + response);
    response = "";
  } else {
    String errorName = getHttpClientErrorName(httpCode);
    Serial.println("   ⚠ Failed to report IR status (HTTP " + String(httpCode) + " / " + errorName + ")");
  }

  http.end();
  payload = "";
  yield();
}

// ========== Forward Declaration ==========
void handleBackendResponse(String response, bool isRfid = false, String rfidUid = "");

void readAndProcessRFID() {
  // Yield to prevent watchdog reset - CRITICAL
  yield();
  
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
  
  // Build UID string
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    uid += String(rfid.uid.uidByte[i] < 0x10 ? "0" : "");
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  
  yield();  // Yield after UID processing
  
  // ========== TAP-TO-CLOSE CHECK (must happen BEFORE debounce) ==========
  // If the same guest card taps again while its locker is in guest tap-mode, lock it locally
  if (uid == lastUnlockedGuestUID && lastUnlockedGuestLockerIndex >= 0 && guestTapUnlock[lastUnlockedGuestLockerIndex]) {
    Serial.println("   🔐 Guest RFID tap detected for currently unlocked locker. Locking locker " + String(lastUnlockedGuestLockerIndex + 1) + "...");
    lockLockerRelay(lastUnlockedGuestLockerIndex);
    showSuccess("LOCKER " + String(lastUnlockedGuestLockerIndex + 1) + " LOCKED");
    lastScannedUID = uid;
    lastRfidRead = millis();
    rfid.PICC_HaltA();
    return;
  }
  
  // ========== DEBOUNCE CHECK (after tap-to-close so it doesn't block close actions) ==========
  // Debounce: don't read too frequently
  if (millis() - lastRfidRead < RFID_READ_DEBOUNCE) {
    rfid.PICC_HaltA();
    return;
  }

  // Skip if same card as last read too quickly
  if (uid == lastScannedUID && millis() - lastRfidRead < 2000) {
    rfid.PICC_HaltA();
    return;
  }
  lastScannedUID = uid;
  lastRfidRead = millis();
  
  // Debug: Check if RFID is being polled
  static unsigned long lastRfidPollDebug = 0;
  if (millis() - lastRfidPollDebug > 5000) {  // Every 5 seconds
    Serial.println("🔍 RFID polling active...");
    lastRfidPollDebug = millis();
  }
  
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
  currentRfidUID = uid;

  StaticJsonDocument<200> doc;
  doc["uid"] = uid;
  
  String payload;
  serializeJson(doc, payload);
  Serial.println("   Payload: " + payload);

  String response;
  int httpCode = postJsonPayload(BACKEND_RFID_URL, payload, response, 2000);

  if (httpCode >= 200 && httpCode < 300) {
    Serial.println("   ✓ Response: " + response);
    handleBackendResponse(response, true, currentRfidUID);
    response = "";
  } else if (httpCode > 0) {
    Serial.println("   ✗ Backend returned HTTP " + String(httpCode) + ", not processing unlock.");
    showError("BACKEND ERROR");
  } else {
    String errorName = getHttpClientErrorName(httpCode);
    Serial.println("   ✗ HTTP Error: " + String(httpCode) + " (" + errorName + ")");
    if (httpCode == -1) {
      showError("BACKEND DOWN");
    } else if (httpCode == -10 || httpCode == -11) {
      showError("HTTP STREAM");
    } else {
      showError("HTTP ERROR");
    }
  }

  payload = "";
  currentRfidUID = "";
  yield();
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
  if (!beginBackendRequest(http, BACKEND_SCAN_ENABLED_URL, 1000)) {
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
    // Minimal cooldown to prevent duplicate reads without blocking the main loop
    lastFingerprintPoll = millis() + 300;
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
  if (!beginBackendRequest(http, BACKEND_FINGERPRINT_URL, 2000)) {
    http.end();
    showError("CONNECTION FAILED");
    return;
  }

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
    String errorName = getHttpClientErrorName(httpCode);
    Serial.println("   HTTP error code: " + String(httpCode) + " (" + errorName + ")");
    showError("HTTP ERROR");
  }

  http.end();
  payload = "";
}


// ========== Handle Backend Response ==========
void handleBackendResponse(String response, bool isRfid, String rfidUid) {
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
      if (isRfid && rfidUid.length() > 0) {
        lastUnlockedGuestUID = rfidUid;
        lastUnlockedGuestLockerIndex = lockerId - 1;
      }
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
  
  // Mark this locker as unlocked for valid close/lock actions
  lockerUnlocked[relayIndex] = true;
  
  // Unlock by toggling to active state
  if (inverted) {
    digitalWrite(pin, HIGH);  // Inverted: HIGH = unlocked
  } else {
    digitalWrite(pin, LOW);   // Normal: LOW = unlocked
  }
}

// ========== Locker lock helper for valid lock actions ==========
void lockLockerRelay(int relayIndex) {
  if (relayIndex < 0 || relayIndex > 3 || !lockerUnlocked[relayIndex]) {
    return;
  }
  
  int pin = relayPins[relayIndex];
  bool inverted = relayInverted[relayIndex];
  
  Serial.println("   🔒 Locking relay " + String(relayIndex + 1));
  
  // Lock by toggling back to locked state
  if (inverted) {
    digitalWrite(pin, LOW);   // Inverted: LOW = locked
  } else {
    digitalWrite(pin, HIGH);  // Normal: HIGH = locked
  }
  
  // Clear unlock flags for this locker
  lockerUnlocked[relayIndex] = false;
  
  // Clear guest tap-to-toggle state when the locker gets locked
  if (lastUnlockedGuestLockerIndex == relayIndex) {
    lastUnlockedGuestUID = "";
    lastUnlockedGuestLockerIndex = -1;
  }
  guestTapUnlock[relayIndex] = false;
  
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

// ========== ESP32 HTTP Control Endpoints ==========

void sendJsonResponse(int code, const String &payload) {
  webServer.send(code, "application/json", payload);
}

bool parseLockerUri(int &lockerId, String &action) {
  String uri = webServer.uri();
  if (!uri.startsWith("/locker/")) {
    return false;
  }

  String remainder = uri.substring(strlen("/locker/"));
  int slashPos = remainder.indexOf('/');
  if (slashPos < 0) {
    return false;
  }

  String idPart = remainder.substring(0, slashPos);
  action = remainder.substring(slashPos + 1);
  lockerId = idPart.toInt();
  return lockerId >= 1 && lockerId <= 4;
}

void handleLockerEndpoint() {
  int lockerId = 0;
  String action;

  if (!parseLockerUri(lockerId, action)) {
    sendJsonResponse(404, "{\"error\":\"not_found\"}");
    return;
  }

  int relayIndex = lockerId - 1;
  if (action == "status") {
    if (webServer.method() != HTTP_GET) {
      sendJsonResponse(405, "{\"error\":\"method_not_allowed\"}");
      return;
    }

    StaticJsonDocument<256> doc;
    doc["status"] = lockerUnlocked[relayIndex] ? "unlocked" : "locked";
    doc["locker_id"] = lockerId;
    doc["locked"] = !lockerUnlocked[relayIndex];
    doc["item_detected"] = lockerOccupied[relayIndex];

    String response;
    serializeJson(doc, response);
    sendJsonResponse(200, response);
    return;
  }

  if (webServer.method() != HTTP_POST) {
    sendJsonResponse(405, "{\"error\":\"method_not_allowed\"}");
    return;
  }

  if (action == "lock") {
    // Manual lock command
    int pin = relayPins[relayIndex];
    bool inverted = relayInverted[relayIndex];
    
    Serial.println("   🔒 Manual lock command received for locker " + String(lockerId));
    
    // Lock the solenoid
    if (inverted) {
      digitalWrite(pin, LOW);   // Inverted: LOW = locked
    } else {
      digitalWrite(pin, HIGH);  // Normal: HIGH = locked
    }
    
    // Clear unlock state
    lockerUnlocked[relayIndex] = false;
    guestTapUnlock[relayIndex] = false;
    lastScannedUID = "";
    
    Serial.println("   ✓ Locker " + String(lockerId) + " LOCKED by manual command");

    StaticJsonDocument<256> doc;
    doc["status"] = "locked";
    doc["locker_id"] = lockerId;
    doc["locked"] = true;
    doc["item_detected"] = lockerOccupied[relayIndex];

    String response;
    serializeJson(doc, response);
    sendJsonResponse(200, response);
    return;
  }

  if (action == "unlock") {
    activateRelay(relayIndex);

    StaticJsonDocument<256> doc;
    doc["status"] = "unlocked";
    doc["locker_id"] = lockerId;
    doc["locked"] = false;
    doc["item_detected"] = lockerOccupied[relayIndex];

    String response;
    serializeJson(doc, response);
    sendJsonResponse(200, response);
    return;
  }

  sendJsonResponse(404, "{\"error\":\"not_found\"}");
}

void setupWebServer() {
  webServer.on("/health", HTTP_GET, []() {
    StaticJsonDocument<512> doc;
    doc["status"] = "ok";
    doc["ip"] = WiFi.localIP().toString();
    doc["ssid"] = WiFi.SSID();
    doc["rssi"] = WiFi.RSSI();
    String response;
    serializeJson(doc, response);
    sendJsonResponse(200, response);
  });

  webServer.on("/fingerprint/start-enrollment", HTTP_POST, []() {
    Serial.println("📩 Direct backend enroll command received via POST");
    if (!enrollmentMode) {
      startFingerprintEnrollment();
      sendJsonResponse(200, "{\"status\":\"enrollment_started\"}");
      return;
    }
    sendJsonResponse(200, "{\"status\":\"already_active\"}");
  });

  webServer.on("/fingerprint/start-enrollment", HTTP_GET, []() {
    Serial.println("📩 Direct backend enroll command received via GET");
    if (!enrollmentMode) {
      startFingerprintEnrollment();
      sendJsonResponse(200, "{\"status\":\"enrollment_started\"}");
      return;
    }
    sendJsonResponse(200, "{\"status\":\"already_active\"}");
  });

  webServer.on("/fingerprint/stop-enrollment", HTTP_POST, []() {
    Serial.println("📩 Direct backend stop-enrollment command received");
    if (enrollmentMode) {
      stopFingerprintEnrollment();
      sendJsonResponse(200, "{\"status\":\"enrollment_stopped\"}");
      return;
    }
    sendJsonResponse(200, "{\"status\":\"already_inactive\"}");
  });

  webServer.onNotFound(handleLockerEndpoint);
  webServer.begin();
  Serial.println("🌐 ESP32 HTTP server started on port 80");
  Serial.println("   📍 Access at: http://" + WiFi.localIP().toString());
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

bool isDuplicateFingerprint() {
  uint8_t p = finger.fingerSearch();
  if (p == FINGERPRINT_OK) {
    Serial.println("   ⚠ Duplicate fingerprint detected during enrollment.");
    return true;
  }
  if (p == FINGERPRINT_NOTFOUND) {
    return false;
  }

  Serial.print("   ⚠ Finger search error during duplicate check: 0x");
  Serial.println(p, HEX);
  return false;
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

        // Check for duplicate fingerprint before creating and storing a new model.
        if (isDuplicateFingerprint()) {
          Serial.println("   ✗ Enrollment cancelled because fingerprint is already registered.");
          showError("DUPLICATE FINGERPRINT");
          stopFingerprintEnrollment();
          break;
        }

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
      String errorMsg = respDoc["error"] | "";
      if (status == "first_scan_complete" || status == "enrolled") {
        Serial.println("   ✓ Fingerprint enrollment step successfully reported to backend.");
        success = true;
      } else if (errorMsg == "duplicate_fingerprint") {
        Serial.println("   ⚠ Duplicate fingerprint detected. Deleting template and stopping enrollment.");
        // Delete the newly stored template
        if (step == 2 && templateId.toInt() > 0) {
          uint8_t deleteResult = finger.deleteModel(templateId.toInt());
          if (deleteResult == FINGERPRINT_OK) {
            Serial.println("   ✓ Template deleted successfully.");
          } else {
            Serial.println("   ✗ Failed to delete template.");
          }
        }
        showError("DUPLICATE FINGERPRINT");
        stopFingerprintEnrollment();
      } else {
        Serial.println("   ✗ Unexpected enrollment response status: " + status + ", error: " + errorMsg);
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
    Serial.println("✗ Failed to begin enrollment polling to backend URL: " + String(BACKEND_FINGERPRINT_START_ENROLL_URL));
    http.end();
    return;
  }

  int httpCode = http.POST("{}");
  Serial.println("   Enrollment poll HTTP code: " + String(httpCode));

  if (httpCode == 200) {
    String response = http.getString();
    Serial.println("   Enrollment poll response: " + response);

    StaticJsonDocument<128> doc;
    DeserializationError error = deserializeJson(doc, response);
    if (!error) {
      const char* status = doc["status"];

      if (status && strcmp(status, "enrollment_started") == 0 && !enrollmentMode) {
        Serial.println("   Backend requested enrollment mode start");
        startFingerprintEnrollment();
      } else if (status && strcmp(status, "enrollment_stopped") == 0 && enrollmentMode) {
        Serial.println("   Backend requested enrollment mode stop");
        stopFingerprintEnrollment();
      } else {
        Serial.println("   Enrollment poll no_action or unknown status: " + String(status ? status : "<none>"));
      }
    } else {
      Serial.println("   Failed to parse enrollment poll JSON response");
    }
  } else {
    Serial.println("   Enrollment poll HTTP failure code: " + String(httpCode));
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