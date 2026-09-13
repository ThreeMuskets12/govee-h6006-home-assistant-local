/*
 * ESP32 Govee H6006 Multi-Bulb Controller
 * Using NimBLE for production-grade stability
 * Serial-only interface (no WiFi/HTTP)
 * 
 * REQUIRES: NimBLE-Arduino library
 * Install via Arduino Library Manager: "NimBLE-Arduino" by h2zero
 */

#include <NimBLEDevice.h>
#include <Preferences.h>
#include <string>

// Set to 1 for debug output, 0 for production (JSON responses only)
#define DEBUG 0

#if DEBUG
  #define DEBUG_PRINT(x) Serial.print(x)
  #define DEBUG_PRINTLN(x) Serial.println(x)
#else
  #define DEBUG_PRINT(x)
  #define DEBUG_PRINTLN(x)
#endif

#define GOVEE_PACKET_SIZE 20
#define KEEPALIVE_INTERVAL 3000
#define MAX_BULBS 4
#define CONNECT_TIMEOUT 5000
#define WRITE_TIMEOUT 3000
#define MAX_RETRY_ATTEMPTS 3

const uint32_t WHITE_VALUES[142] = {
    0xff8d0b, 0xff8912, 0xff921d, 0xff8e21, 0xff9829, 0xff932c, 0xff9d33, 0xff9836,
    0xffa23c, 0xff9d3f, 0xffa645, 0xffa148, 0xffaa4d, 0xffa54f, 0xffae54, 0xffa957,
    0xffb25b, 0xffad5e, 0xffb662, 0xffb165, 0xffb969, 0xffb46b, 0xffbd6f, 0xffb872,
    0xffc076, 0xffbb78, 0xffc37c, 0xffbe7e, 0xffc682, 0xffc184, 0xffc987, 0xffc489,
    0xffcb8d, 0xffc78f, 0xffce92, 0xffc994, 0xffd097, 0xffcc99, 0xffd39c, 0xffce9f,
    0xffd5a1, 0xffd1a3, 0xffd7a6, 0xffd3a8, 0xffd9ab, 0xffd5ad, 0xffdbaf, 0xffd7b1,
    0xffddb4, 0xffd9b6, 0xffdfb8, 0xffdbba, 0xffe1bc, 0xffddbe, 0xffe2c0, 0xffdfc2,
    0xffe4c4, 0xffe1c6, 0xffe5c8, 0xffe3ca, 0xffe7cc, 0xffe4ce, 0xffe8d0, 0xffe6d2,
    0xffead3, 0xffe8d5, 0xffebd7, 0xffe9d9, 0xffedda, 0xffebdc, 0xffeede, 0xffece0,
    0xffefe1, 0xffeee3, 0xfff0e4, 0xffefe6, 0xfff1e7, 0xfff0e9, 0xfff3ea, 0xfff2ec,
    0xfff4ed, 0xfff3ef, 0xfff5f0, 0xfff4f2, 0xfff6f3, 0xfff5f5, 0xfff7f7, 0xfff6f8,
    0xfff8f8, 0xfff8fb, 0xfff9fb, 0xfff9fd, 0xfff9fd, 0xfef9ff, 0xfefaff, 0xfcf7ff,
    0xfcf8ff, 0xf9f6ff, 0xfaf7ff, 0xf7f5ff, 0xf7f5ff, 0xf5f3ff, 0xf5f4ff, 0xf3f2ff,
    0xf3f3ff, 0xf0f1ff, 0xf1f1ff, 0xeff0ff, 0xeff0ff, 0xedefff, 0xeeefff, 0xebeeff,
    0xeceeff, 0xe9edff, 0xeaedff, 0xe7ecff, 0xe9ecff, 0xe6ebff, 0xe7eaff, 0xe4eaff,
    0xe5e9ff, 0xe3e9ff, 0xe4e9ff, 0xe1e8ff, 0xe3e8ff, 0xe0e7ff, 0xe1e7ff, 0xdee6ff,
    0xe0e6ff, 0xdde6ff, 0xdfe5ff, 0xdce5ff, 0xdde4ff, 0xdae4ff, 0xdce3ff, 0xd9e3ff,
    0xdbe2ff, 0xd8e3ff, 0xdae2ff, 0xd7e2ff, 0xd9e1ff, 0xd6e1ff
};

// UUIDs
static NimBLEUUID goveeServiceUUID("00010203-0405-0607-0809-0a0b0c0d1910");
static NimBLEUUID goveeWriteUUID("00010203-0405-0607-0809-0a0b0c0d2b11");

// Forward declaration of RequestResult struct (needed before use)
struct RequestResult {
    int statusCode;
    String response;
};

// Bulb structure
struct GoveeBulb {
    NimBLEClient* client;
    NimBLERemoteCharacteristic* writeChar;
    NimBLEAddress address;
    String name;
    bool connected;
    unsigned long lastKeepAlive;
    int consecutiveFailures;
    // Reconnection backoff tracking
    int reconnectAttempts;
    unsigned long lastReconnectAttempt;
    bool slowReconnectMode;
};

#define RECONNECT_FAST_ATTEMPTS 3      // Number of quick reconnect attempts before backing off
#define RECONNECT_SLOW_INTERVAL 60000  // 1 minute between reconnect attempts in slow mode

// Globals
NimBLEScan* pBLEScan;
GoveeBulb bulbs[MAX_BULBS];
int numBulbs = 0;

// Scan results storage
struct ScannedDevice {
    NimBLEAddress address;
    String name;
};
ScannedDevice bleDevices[50];
int bleCount = 0;
bool scanComplete = false;

bool setupComplete = false;
Preferences preferences;

// Mutex for BLE operations
SemaphoreHandle_t bleMutex;

// ========== NimBLE Scan Callback ==========

class ScanCallbacks : public NimBLEScanCallbacks {
public:
    void onResult(const NimBLEAdvertisedDevice* device) override {
        if(device == nullptr) {
            DEBUG_PRINTLN("      [CALLBACK] onResult called with nullptr!");
            return;
        }
        
        if(bleCount < 50) {
            bleDevices[bleCount].address = device->getAddress();
            bleDevices[bleCount].name = device->getName().c_str();
            
            DEBUG_PRINT("      Found: ");
            if(bleDevices[bleCount].name.length() > 0) {
                DEBUG_PRINT(bleDevices[bleCount].name);
            } else {
                DEBUG_PRINT("(unnamed)");
            }
            DEBUG_PRINT(" [");
            DEBUG_PRINT(bleDevices[bleCount].address.toString().c_str());
            DEBUG_PRINTLN("]");
            
            bleCount++;
        }
    }
    
    void onScanEnd(const NimBLEScanResults& results, int reason) override {
        DEBUG_PRINT("      [CALLBACK] Scan ended, found ");
        DEBUG_PRINT(results.getCount());
        DEBUG_PRINT(" devices, reason: ");
        DEBUG_PRINTLN(reason);
        scanComplete = true;
    }
};

static ScanCallbacks scanCallbacks;

// ========== NimBLE Client Callbacks ==========

class ClientCallbacks : public NimBLEClientCallbacks {
    void onConnect(NimBLEClient* pClient) override {
        DEBUG_PRINT("[BLE] Connected to ");
        DEBUG_PRINTLN(pClient->getPeerAddress().toString().c_str());
    }
    
    void onDisconnect(NimBLEClient* pClient, int reason) override {
        DEBUG_PRINT("[BLE] Disconnected from ");
        DEBUG_PRINT(pClient->getPeerAddress().toString().c_str());
        DEBUG_PRINT(" reason=");
        DEBUG_PRINTLN(reason);
        
        for(int i = 0; i < numBulbs; i++) {
            if(bulbs[i].client == pClient) {
                bulbs[i].connected = false;
                bulbs[i].writeChar = nullptr;
                DEBUG_PRINT("[BLE] Marked bulb ");
                DEBUG_PRINT(i);
                DEBUG_PRINTLN(" as disconnected");
                break;
            }
        }
    }
};

static ClientCallbacks clientCallbacks;

// ========== Govee Protocol ==========

uint8_t govee_calculate_checksum(const uint8_t* packet) {
    uint8_t checksum = 0;
    for(int i = 0; i < GOVEE_PACKET_SIZE - 1; i++) {
        checksum ^= packet[i];
    }
    return checksum;
}

void govee_build_power_packet(uint8_t* packet, bool on) {
    memset(packet, 0, GOVEE_PACKET_SIZE);
    packet[0] = 0x33;
    packet[1] = 0x01;
    packet[2] = on ? 0x01 : 0x00;
    packet[19] = govee_calculate_checksum(packet);
}

void govee_build_brightness_packet(uint8_t* packet, uint8_t brightness) {
    memset(packet, 0, GOVEE_PACKET_SIZE);
    packet[0] = 0x33;
    packet[1] = 0x04;
    packet[2] = brightness;
    packet[19] = govee_calculate_checksum(packet);
}

void govee_build_color_packet(uint8_t* packet, uint8_t r, uint8_t g, uint8_t b) {
    memset(packet, 0, GOVEE_PACKET_SIZE);
    packet[0] = 0x33;
    packet[1] = 0x05;
    packet[2] = 0x0D;
    packet[3] = r;
    packet[4] = g;
    packet[5] = b;
    packet[19] = govee_calculate_checksum(packet);
}

void govee_build_temperature_packet(uint8_t* packet, uint16_t temperature) {
    uint8_t white_index;
    if (temperature <= 2000) {
        white_index = 0;
    } else if (temperature >= 9000) {
        white_index = 140;
    } else {
        white_index = (uint8_t)(((uint32_t)(temperature - 2000) * 140) / 7000);
    }
    uint32_t white_value = WHITE_VALUES[white_index];
    uint16_t kelvin_value = (white_index * 50) + 2000;

    memset(packet, 0, GOVEE_PACKET_SIZE);
    packet[0] = 0x33;
    packet[1] = 0x05;
    packet[2] = 0x0D;
    packet[3] = (white_value >> 16) & 0xFF;
    packet[4] = (white_value >> 8) & 0xFF;
    packet[5] = white_value & 0xFF;
    packet[6] = (kelvin_value >> 8) & 0xFF;
    packet[7] = kelvin_value & 0xFF;
    packet[8] = (white_value >> 16) & 0xFF;
    packet[9] = (white_value >> 8) & 0xFF;
    packet[10] = white_value & 0xFF;
    packet[19] = govee_calculate_checksum(packet);
}

void govee_build_keepalive_packet(uint8_t* packet) {
    memset(packet, 0, GOVEE_PACKET_SIZE);
    packet[0] = 0xAA;
    packet[1] = 0x01;
    packet[2] = 0x00;
    packet[19] = govee_calculate_checksum(packet);
}

// ========== Non-Volatile Storage ==========

void saveConfig() {
    DEBUG_PRINTLN("\n[CONFIG] Saving configuration...");
    
    preferences.begin("govee", false);
    preferences.putInt("numBulbs", numBulbs);
    
    for(int i = 0; i < numBulbs; i++) {
        String macKey = "mac" + String(i);
        String nameKey = "name" + String(i);
        preferences.putString(macKey.c_str(), bulbs[i].address.toString().c_str());
        preferences.putString(nameKey.c_str(), bulbs[i].name);
    }
    
    preferences.end();
    DEBUG_PRINTLN("[CONFIG] Configuration saved!");
}

bool loadConfig() {
    preferences.begin("govee", true);
    
    if(!preferences.isKey("numBulbs")) {
        preferences.end();
        return false;
    }
    
    DEBUG_PRINTLN("\n[CONFIG] Found saved configuration");
    
    numBulbs = preferences.getInt("numBulbs", 0);
    
    DEBUG_PRINT("[CONFIG] Bulbs: ");
    DEBUG_PRINTLN(numBulbs);
    
    for(int i = 0; i < numBulbs; i++) {
        String macKey = "mac" + String(i);
        String nameKey = "name" + String(i);
        String mac = preferences.getString(macKey.c_str(), "");
        String name = preferences.getString(nameKey.c_str(), "");
        
        bulbs[i].address = NimBLEAddress(std::string(mac.c_str()), 0);
        bulbs[i].name = name;
        bulbs[i].client = nullptr;
        bulbs[i].writeChar = nullptr;
        bulbs[i].connected = false;
        bulbs[i].consecutiveFailures = 0;
        bulbs[i].reconnectAttempts = 0;
        bulbs[i].lastReconnectAttempt = 0;
        bulbs[i].slowReconnectMode = false;
        
        DEBUG_PRINT("[CONFIG]   Bulb ");
        DEBUG_PRINT(i);
        DEBUG_PRINT(": ");
        DEBUG_PRINT(name);
        DEBUG_PRINT(" (");
        DEBUG_PRINT(mac);
        DEBUG_PRINTLN(")");
    }
    
    preferences.end();
    return true;
}

void clearConfig() {
    preferences.begin("govee", false);
    preferences.clear();
    preferences.end();
    DEBUG_PRINTLN("[CONFIG] Configuration cleared");
}

// ========== Scanning Functions ==========

void scanBLEDevices() {
    DEBUG_PRINTLN("[SCAN] Scanning BLE devices...");
    bleCount = 0;
    scanComplete = false;
    
    pBLEScan->clearResults();
    
    DEBUG_PRINTLN("      Starting scan for 5 seconds...");
    
    bool started = pBLEScan->start(0);
    DEBUG_PRINT("      Scan started: ");
    DEBUG_PRINTLN(started ? "YES" : "NO");
    
    if(!started) {
        DEBUG_PRINTLN("      ERROR: Failed to start scan!");
        return;
    }
    
    delay(5000);
    
    DEBUG_PRINTLN("      Stopping scan...");
    pBLEScan->stop();
    
    delay(500);
    
    DEBUG_PRINT("      Total: ");
    DEBUG_PRINT(bleCount);
    DEBUG_PRINTLN(" BLE devices found");
}

// ========== Bulb Connection ==========

bool connectToBulb(int bulbIdx) {
    if(xSemaphoreTake(bleMutex, pdMS_TO_TICKS(10000)) != pdTRUE) {
        DEBUG_PRINTLN("[BLE] Could not acquire mutex for connection");
        return false;
    }
    
    // Track reconnect attempt
    bulbs[bulbIdx].lastReconnectAttempt = millis();
    bulbs[bulbIdx].reconnectAttempts++;
    
    DEBUG_PRINT("\n[BLE] Connecting to bulb ");
    DEBUG_PRINT(bulbIdx);
    DEBUG_PRINT(" (");
    DEBUG_PRINT(bulbs[bulbIdx].name);
    DEBUG_PRINT(") attempt ");
    DEBUG_PRINT(bulbs[bulbIdx].reconnectAttempts);
    DEBUG_PRINT("...");
    
    if(bulbs[bulbIdx].client != nullptr) {
        if(bulbs[bulbIdx].client->isConnected()) {
            bulbs[bulbIdx].client->disconnect();
        }
        NimBLEDevice::deleteClient(bulbs[bulbIdx].client);
        bulbs[bulbIdx].client = nullptr;
    }
    bulbs[bulbIdx].writeChar = nullptr;
    bulbs[bulbIdx].connected = false;
    
    bulbs[bulbIdx].client = NimBLEDevice::createClient();
    bulbs[bulbIdx].client->setClientCallbacks(&clientCallbacks, false);
    
    if(!bulbs[bulbIdx].client->connect(bulbs[bulbIdx].address, false)) {
        DEBUG_PRINTLN(" FAILED - Could not connect");
        NimBLEDevice::deleteClient(bulbs[bulbIdx].client);
        bulbs[bulbIdx].client = nullptr;
        
        // Check if we should enter slow reconnect mode
        if(bulbs[bulbIdx].reconnectAttempts >= RECONNECT_FAST_ATTEMPTS) {
            if(!bulbs[bulbIdx].slowReconnectMode) {
                DEBUG_PRINT("[BLE] Bulb ");
                DEBUG_PRINT(bulbIdx);
                DEBUG_PRINTLN(" entering slow reconnect mode (1 attempt/minute)");
                bulbs[bulbIdx].slowReconnectMode = true;
            }
        }
        
        xSemaphoreGive(bleMutex);
        return false;
    }
    
    NimBLERemoteService* service = bulbs[bulbIdx].client->getService(goveeServiceUUID);
    if(service == nullptr) {
        DEBUG_PRINTLN(" FAILED - Service not found");
        bulbs[bulbIdx].client->disconnect();
        NimBLEDevice::deleteClient(bulbs[bulbIdx].client);
        bulbs[bulbIdx].client = nullptr;
        
        if(bulbs[bulbIdx].reconnectAttempts >= RECONNECT_FAST_ATTEMPTS) {
            if(!bulbs[bulbIdx].slowReconnectMode) {
                DEBUG_PRINT("[BLE] Bulb ");
                DEBUG_PRINT(bulbIdx);
                DEBUG_PRINTLN(" entering slow reconnect mode (1 attempt/minute)");
                bulbs[bulbIdx].slowReconnectMode = true;
            }
        }
        
        xSemaphoreGive(bleMutex);
        return false;
    }
    
    bulbs[bulbIdx].writeChar = service->getCharacteristic(goveeWriteUUID);
    if(bulbs[bulbIdx].writeChar == nullptr) {
        DEBUG_PRINTLN(" FAILED - Characteristic not found");
        bulbs[bulbIdx].client->disconnect();
        NimBLEDevice::deleteClient(bulbs[bulbIdx].client);
        bulbs[bulbIdx].client = nullptr;
        
        if(bulbs[bulbIdx].reconnectAttempts >= RECONNECT_FAST_ATTEMPTS) {
            if(!bulbs[bulbIdx].slowReconnectMode) {
                DEBUG_PRINT("[BLE] Bulb ");
                DEBUG_PRINT(bulbIdx);
                DEBUG_PRINTLN(" entering slow reconnect mode (1 attempt/minute)");
                bulbs[bulbIdx].slowReconnectMode = true;
            }
        }
        
        xSemaphoreGive(bleMutex);
        return false;
    }
    
    bulbs[bulbIdx].connected = true;
    bulbs[bulbIdx].lastKeepAlive = millis();
    bulbs[bulbIdx].consecutiveFailures = 0;
    
    // Reset reconnect tracking on successful connection
    bulbs[bulbIdx].reconnectAttempts = 0;
    bulbs[bulbIdx].slowReconnectMode = false;
    
    DEBUG_PRINTLN(" SUCCESS!");
    
    xSemaphoreGive(bleMutex);
    return true;
}

void selectBulbs() {
    DEBUG_PRINTLN("\n=== BLE Devices ===");
    
    for(int i = 0; i < bleCount; i++) {
        DEBUG_PRINT(i);
        DEBUG_PRINT(": ");
        DEBUG_PRINT(bleDevices[i].name);
        DEBUG_PRINT(" (");
        DEBUG_PRINT(bleDevices[i].address.toString().c_str());
        DEBUG_PRINTLN(")");
    }
    
    while(numBulbs < MAX_BULBS) {
        DEBUG_PRINT("\nSelect device number (or -1 to finish): ");
        while(!Serial.available()) delay(100);
        int selection = Serial.parseInt();
        while(Serial.available()) Serial.read();
        DEBUG_PRINTLN(selection);
        
        if(selection == -1) {
            if(numBulbs == 0) {
                DEBUG_PRINTLN("ERROR: Must select at least one bulb");
                continue;
            }
            break;
        }
        
        if(selection < 0 || selection >= bleCount) {
            DEBUG_PRINTLN("ERROR: Invalid selection");
            continue;
        }
        
        bool alreadySelected = false;
        for(int i = 0; i < numBulbs; i++) {
            if(bulbs[i].address == bleDevices[selection].address) {
                DEBUG_PRINTLN("ERROR: Bulb already selected");
                alreadySelected = true;
                break;
            }
        }
        if(alreadySelected) continue;
        
        DEBUG_PRINT("Enter name for this bulb: ");
        while(!Serial.available()) delay(100);
        String bulbName = Serial.readStringUntil('\n');
        bulbName.trim();
        DEBUG_PRINTLN(bulbName);
        
        if(bulbName.length() == 0) {
            DEBUG_PRINTLN("ERROR: Name cannot be empty");
            continue;
        }
        
        bulbs[numBulbs].address = bleDevices[selection].address;
        bulbs[numBulbs].name = bulbName;
        bulbs[numBulbs].consecutiveFailures = 0;
        bulbs[numBulbs].reconnectAttempts = 0;
        bulbs[numBulbs].lastReconnectAttempt = 0;
        bulbs[numBulbs].slowReconnectMode = false;
        
        if(connectToBulb(numBulbs)) {
            numBulbs++;
            DEBUG_PRINT("Connected bulbs: ");
            DEBUG_PRINT(numBulbs);
            DEBUG_PRINT("/");
            DEBUG_PRINTLN(MAX_BULBS);
        }
    }
}

// ========== Command Functions ==========

bool writeWithRetry(int idx, uint8_t* packet, const char* cmdName) {
    if(idx < 0 || idx >= numBulbs) {
        DEBUG_PRINTLN("ERROR: Invalid bulb number");
        return false;
    }
    
    // If bulb is in slow reconnect mode, only try once (don't do rapid retries)
    int maxAttempts = bulbs[idx].slowReconnectMode ? 1 : MAX_RETRY_ATTEMPTS;
    
    for(int attempt = 0; attempt < maxAttempts; attempt++) {
        if(!bulbs[idx].connected || bulbs[idx].client == nullptr || 
           !bulbs[idx].client->isConnected() || bulbs[idx].writeChar == nullptr) {
            
            // If in slow reconnect mode, check if enough time has passed
            if(bulbs[idx].slowReconnectMode) {
                unsigned long now = millis();
                if(now - bulbs[idx].lastReconnectAttempt < RECONNECT_SLOW_INTERVAL) {
                    DEBUG_PRINT("[");
                    DEBUG_PRINT(cmdName);
                    DEBUG_PRINT("] Bulb ");
                    DEBUG_PRINT(idx);
                    DEBUG_PRINTLN(" in slow reconnect mode, skipping rapid retry");
                    return false;
                }
            }
            
            DEBUG_PRINT("[");
            DEBUG_PRINT(cmdName);
            DEBUG_PRINT("] Bulb ");
            DEBUG_PRINT(idx);
            DEBUG_PRINT(" not connected, reconnecting (attempt ");
            DEBUG_PRINT(attempt + 1);
            DEBUG_PRINTLN(")...");
            
            if(!connectToBulb(idx)) {
                DEBUG_PRINTLN("Reconnection failed, will retry...");
                delay(500);
                continue;
            }
        }
        
        if(xSemaphoreTake(bleMutex, pdMS_TO_TICKS(WRITE_TIMEOUT)) != pdTRUE) {
            DEBUG_PRINTLN("[BLE] Could not acquire mutex for write");
            continue;
        }
        
        DEBUG_PRINT("[");
        DEBUG_PRINT(cmdName);
        DEBUG_PRINT("] Writing to bulb ");
        DEBUG_PRINT(idx);
        DEBUG_PRINT("...");
        
        bool success = bulbs[idx].writeChar->writeValue(packet, GOVEE_PACKET_SIZE, false);
        
        xSemaphoreGive(bleMutex);
        
        if(success) {
            DEBUG_PRINTLN(" SUCCESS!");
            bulbs[idx].consecutiveFailures = 0;
            return true;
        } else {
            DEBUG_PRINTLN(" FAILED!");
            bulbs[idx].consecutiveFailures++;
            bulbs[idx].connected = false;
            delay(200);
        }
    }
    
    DEBUG_PRINT("[");
    DEBUG_PRINT(cmdName);
    DEBUG_PRINTLN("] All retry attempts exhausted");
    return false;
}

void sendKeepAlive(int idx) {
    if(!bulbs[idx].connected || bulbs[idx].writeChar == nullptr) return;
    
    uint8_t packet[GOVEE_PACKET_SIZE];
    govee_build_keepalive_packet(packet);
    
    if(xSemaphoreTake(bleMutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        return;
    }
    
    bool success = bulbs[idx].writeChar->writeValue(packet, GOVEE_PACKET_SIZE, false);
    
    xSemaphoreGive(bleMutex);
    
    if(success) {
        DEBUG_PRINT(".");
        bulbs[idx].consecutiveFailures = 0;
    } else {
        DEBUG_PRINT("\n[KEEPALIVE] Bulb ");
        DEBUG_PRINT(idx);
        DEBUG_PRINTLN(" failed");
        bulbs[idx].consecutiveFailures++;
        
        if(bulbs[idx].consecutiveFailures >= 3) {
            DEBUG_PRINT("[KEEPALIVE] Too many failures, marking bulb ");
            DEBUG_PRINT(idx);
            DEBUG_PRINTLN(" for reconnection");
            bulbs[idx].connected = false;
        }
    }
}

bool sendPowerCommand(int idx, bool on) {
    uint8_t packet[GOVEE_PACKET_SIZE];
    govee_build_power_packet(packet, on);
    return writeWithRetry(idx, packet, "POWER");
}

bool sendBrightnessCommand(int idx, int brightnessPercent) {
    if(brightnessPercent < 0 || brightnessPercent > 100) {
        DEBUG_PRINTLN("ERROR: Brightness must be 0-100");
        return false;
    }
    
    uint8_t brightness = map(brightnessPercent, 0, 100, 1, 243);
    uint8_t packet[GOVEE_PACKET_SIZE];
    govee_build_brightness_packet(packet, brightness);
    return writeWithRetry(idx, packet, "BRIGHTNESS");
}

bool sendColorCommand(int idx, uint8_t r, uint8_t g, uint8_t b) {
    uint8_t packet[GOVEE_PACKET_SIZE];
    govee_build_color_packet(packet, r, g, b);
    return writeWithRetry(idx, packet, "COLOR");
}

bool sendTemperatureCommand(int idx, uint16_t temperature) {
    if(temperature < 2000 || temperature > 9000) {
        DEBUG_PRINTLN("ERROR: Temperature must be 2000-9000K");
        return false;
    }
    
    uint8_t packet[GOVEE_PACKET_SIZE];
    govee_build_temperature_packet(packet, temperature);
    return writeWithRetry(idx, packet, "TEMPERATURE");
}

void disconnectBulb(int idx) {
    if(idx < 0 || idx >= numBulbs) return;
    
    DEBUG_PRINT("\n[DISCONNECT] Disconnecting bulb ");
    DEBUG_PRINT(idx);
    DEBUG_PRINTLN("...");
    
    if(xSemaphoreTake(bleMutex, pdMS_TO_TICKS(5000)) == pdTRUE) {
        if(bulbs[idx].client != nullptr) {
            if(bulbs[idx].client->isConnected()) {
                bulbs[idx].client->disconnect();
            }
            NimBLEDevice::deleteClient(bulbs[idx].client);
            bulbs[idx].client = nullptr;
        }
        bulbs[idx].writeChar = nullptr;
        bulbs[idx].connected = false;
        xSemaphoreGive(bleMutex);
    }
    
    DEBUG_PRINTLN("[DISCONNECT] Done");
}

// ========== Unified Request Handler ==========

int findBulbByName(String name) {
    for(int i = 0; i < numBulbs; i++) {
        if(bulbs[i].name.equalsIgnoreCase(name)) {
            return i;
        }
    }
    return -1;
}

String buildBulbsJson() {
    String json = "{\"bulbs\":[";
    
    for(int i = 0; i < numBulbs; i++) {
        if(i > 0) json += ",";
        json += "{";
        json += "\"id\":" + String(i) + ",";
        json += "\"name\":\"" + bulbs[i].name + "\",";
        json += "\"address\":\"" + String(bulbs[i].address.toString().c_str()) + "\",";
        json += "\"connected\":" + String(bulbs[i].connected ? "true" : "false");
        json += "}";
    }
    
    json += "],\"count\":" + String(numBulbs) + "}";
    return json;
}

RequestResult handleRequest(String uri) {
    RequestResult result;
    
    if(uri == "/bulbs" || uri == "/bulbs/") {
        result.statusCode = 200;
        result.response = buildBulbsJson();
        return result;
    }
    
    if(!uri.startsWith("/bulb/")) {
        result.statusCode = 404;
        result.response = "{\"error\":\"Endpoint not found\"}";
        return result;
    }
    
    int firstSlash = uri.indexOf('/', 1);
    int secondSlash = uri.indexOf('/', firstSlash + 1);
    
    if(firstSlash == -1 || secondSlash == -1) {
        result.statusCode = 400;
        result.response = "{\"error\":\"Invalid URI format\"}";
        return result;
    }
    
    String bulbName = uri.substring(firstSlash + 1, secondSlash);
    String remaining = uri.substring(secondSlash + 1);
    bulbName.replace("%20", " ");
    
    int thirdSlash = remaining.indexOf('/');
    String action;
    String valueStr = "";
    
    if(thirdSlash == -1) {
        action = remaining;
    } else {
        action = remaining.substring(0, thirdSlash);
        valueStr = remaining.substring(thirdSlash + 1);
    }
    
    DEBUG_PRINT("[REQUEST] Bulb: ");
    DEBUG_PRINT(bulbName);
    DEBUG_PRINT(", Action: ");
    DEBUG_PRINT(action);
    if(valueStr.length() > 0) {
        DEBUG_PRINT(", Value: ");
        DEBUG_PRINT(valueStr);
    }
    DEBUG_PRINTLN("");
    
    int bulbId = findBulbByName(bulbName);
    
    if(bulbId == -1) {
        result.statusCode = 404;
        result.response = "{\"error\":\"Bulb not found\"}";
        return result;
    }
    
    bool success = false;
    
    if(action == "on") {
        success = sendPowerCommand(bulbId, true);
        result.statusCode = success ? 200 : 500;
        result.response = success ? "{\"success\":true,\"action\":\"on\"}" : "{\"error\":\"Command failed\"}";
    } else if(action == "off") {
        success = sendPowerCommand(bulbId, false);
        result.statusCode = success ? 200 : 500;
        result.response = success ? "{\"success\":true,\"action\":\"off\"}" : "{\"error\":\"Command failed\"}";
    } else if(action == "disconnect") {
        disconnectBulb(bulbId);
        result.statusCode = 200;
        result.response = "{\"success\":true,\"action\":\"disconnect\"}";
    } else if(action == "connect") {
        success = connectToBulb(bulbId);
        result.statusCode = success ? 200 : 500;
        result.response = success ? "{\"success\":true,\"action\":\"connect\"}" : "{\"error\":\"Connection failed\"}";
    } else if(action == "brightness") {
        if(valueStr.length() == 0) {
            result.statusCode = 400;
            result.response = "{\"error\":\"Brightness value required\"}";
            return result;
        }
        int brightness = valueStr.toInt();
        if(brightness < 0 || brightness > 100) {
            result.statusCode = 400;
            result.response = "{\"error\":\"Brightness must be 0-100\"}";
            return result;
        }
        success = sendBrightnessCommand(bulbId, brightness);
        result.statusCode = success ? 200 : 500;
        result.response = success ? "{\"success\":true,\"action\":\"brightness\",\"value\":" + String(brightness) + "}" : "{\"error\":\"Command failed\"}";
    } else if(action == "rgb") {
        if(valueStr.length() == 0) {
            result.statusCode = 400;
            result.response = "{\"error\":\"RGB values required\"}";
            return result;
        }
        
        int r = -1, g = -1, b = -1;
        int rPos = valueStr.indexOf("r=");
        int gPos = valueStr.indexOf("g=");
        int bPos = valueStr.indexOf("b=");
        
        if(rPos != -1) {
            int rEnd = valueStr.indexOf('&', rPos);
            if(rEnd == -1) rEnd = valueStr.length();
            r = valueStr.substring(rPos + 2, rEnd).toInt();
        }
        if(gPos != -1) {
            int gEnd = valueStr.indexOf('&', gPos);
            if(gEnd == -1) gEnd = valueStr.length();
            g = valueStr.substring(gPos + 2, gEnd).toInt();
        }
        if(bPos != -1) {
            int bEnd = valueStr.indexOf('&', bPos);
            if(bEnd == -1) bEnd = valueStr.length();
            b = valueStr.substring(bPos + 2, bEnd).toInt();
        }
        
        if(r < 0 || r > 255 || g < 0 || g > 255 || b < 0 || b > 255) {
            result.statusCode = 400;
            result.response = "{\"error\":\"RGB values must be 0-255\"}";
            return result;
        }
        
        success = sendColorCommand(bulbId, r, g, b);
        result.statusCode = success ? 200 : 500;
        result.response = success ? "{\"success\":true,\"action\":\"rgb\"}" : "{\"error\":\"Command failed\"}";
    } else if(action == "temperature") {
        if(valueStr.length() == 0) {
            result.statusCode = 400;
            result.response = "{\"error\":\"Temperature value required\"}";
            return result;
        }
        int temp = valueStr.toInt();
        if(temp < 2000 || temp > 9000) {
            result.statusCode = 400;
            result.response = "{\"error\":\"Temperature must be 2000-9000K\"}";
            return result;
        }
        success = sendTemperatureCommand(bulbId, temp);
        result.statusCode = success ? 200 : 500;
        result.response = success ? "{\"success\":true,\"action\":\"temperature\",\"value\":" + String(temp) + "}" : "{\"error\":\"Command failed\"}";
    } else {
        result.statusCode = 400;
        result.response = "{\"error\":\"Invalid action\"}";
    }
    
    return result;
}

// ========== Serial Input Handler ==========

void handleSerialInput() {
    if(!Serial.available()) return;
    
    String input = Serial.readStringUntil('\n');
    input.trim();
    
    if(input.length() == 0) return;
    
    DEBUG_PRINT("[SERIAL] Received: '");
    DEBUG_PRINT(input);
    DEBUG_PRINTLN("'");
    
    RequestResult result = handleRequest(input);
    Serial.println(result.response);  // JSON response always printed
}

// ========== Background Reconnection Task ==========

void checkConnectionsTask(void* parameter) {
    for(;;) {
        vTaskDelay(pdMS_TO_TICKS(5000));  // Check every 5 seconds
        
        unsigned long now = millis();
        
        for(int i = 0; i < numBulbs; i++) {
            // Check if bulb needs reconnection
            if(!bulbs[i].connected || bulbs[i].client == nullptr || 
               (bulbs[i].client != nullptr && !bulbs[i].client->isConnected())) {
                
                // Check if we're in slow reconnect mode
                if(bulbs[i].slowReconnectMode) {
                    // Only attempt reconnect once per minute in slow mode
                    if(now - bulbs[i].lastReconnectAttempt < RECONNECT_SLOW_INTERVAL) {
                        continue;  // Skip this bulb, not time yet
                    }
                    DEBUG_PRINT("\n[MONITOR] Bulb ");
                    DEBUG_PRINT(i);
                    DEBUG_PRINTLN(" slow reconnect attempt...");
                } else {
                    DEBUG_PRINT("\n[MONITOR] Bulb ");
                    DEBUG_PRINT(i);
                    DEBUG_PRINTLN(" needs reconnection, attempting...");
                }
                
                connectToBulb(i);
                
                // Small delay between attempts for different bulbs
                vTaskDelay(pdMS_TO_TICKS(500));
            }
        }
    }
}

// ========== Setup & Loop ==========

void setup() {
    Serial.begin(115200);
    delay(1000);
    
    DEBUG_PRINTLN("\n========================================");
    DEBUG_PRINTLN("  ESP32 Govee Controller (Serial Only)");
    DEBUG_PRINTLN("========================================");
    
    bleMutex = xSemaphoreCreateMutex();
    if(bleMutex == NULL) {
        DEBUG_PRINTLN("ERROR: Failed to create mutex!");
        while(1) delay(1000);
    }
    
    for(int i = 0; i < MAX_BULBS; i++) {
        bulbs[i].client = nullptr;
        bulbs[i].writeChar = nullptr;
        bulbs[i].connected = false;
        bulbs[i].consecutiveFailures = 0;
        bulbs[i].reconnectAttempts = 0;
        bulbs[i].lastReconnectAttempt = 0;
        bulbs[i].slowReconnectMode = false;
    }
    
    NimBLEDevice::init("ESP32_Govee");
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    
    pBLEScan = NimBLEDevice::getScan();
    pBLEScan->setScanCallbacks(&scanCallbacks);
    pBLEScan->setActiveScan(true);
    pBLEScan->setInterval(100);
    pBLEScan->setWindow(99);
    
    bool hasConfig = loadConfig();
    bool runSetup = true;
    
    if(hasConfig) {
        DEBUG_PRINTLN("\n[PROMPT] Saved config found. Press 's' within 5 seconds to run setup...");
        
        unsigned long startTime = millis();
        bool userResponded = false;
        
        while(millis() - startTime < 5000) {
            if(Serial.available()) {
                char c = Serial.read();
                if(c == 's' || c == 'S') {
                    userResponded = true;
                    while(Serial.available()) Serial.read();
                    break;
                }
            }
            delay(100);
        }
        
        if(!userResponded) {
            DEBUG_PRINTLN("[AUTO] Using saved configuration");
            runSetup = false;
            
            for(int i = 0; i < numBulbs; i++) {
                connectToBulb(i);
                delay(1000);
            }
        } else {
            DEBUG_PRINTLN("[SETUP] User requested setup mode");
        }
    } else {
        DEBUG_PRINTLN("\n[SETUP] No saved config found. Running setup...");
    }
    
    if(runSetup) {
        scanBLEDevices();
        
        numBulbs = 0;
        selectBulbs();
        saveConfig();
    }
    
    DEBUG_PRINTLN("\n=== Setup Complete ===");
    for(int i = 0; i < numBulbs; i++) {
        DEBUG_PRINT("  Bulb ");
        DEBUG_PRINT(i);
        DEBUG_PRINT(": ");
        DEBUG_PRINT(bulbs[i].name);
        DEBUG_PRINT(" [");
        DEBUG_PRINT(bulbs[i].connected ? "Connected" : "Disconnected");
        DEBUG_PRINTLN("]");
    }
    
    xTaskCreatePinnedToCore(
        checkConnectionsTask,
        "BLEMonitor",
        4096,
        NULL,
        1,
        NULL,
        0
    );
    
    setupComplete = true;
    DEBUG_PRINTLN("\nReady! Serial Commands:");
    DEBUG_PRINTLN("  /bulbs                       - List all bulbs");
    DEBUG_PRINTLN("  /bulb/{name}/on              - Turn on");
    DEBUG_PRINTLN("  /bulb/{name}/off             - Turn off");
    DEBUG_PRINTLN("  /bulb/{name}/brightness/N    - Set brightness (0-100)");
    DEBUG_PRINTLN("  /bulb/{name}/rgb/r=R&g=G&b=B - Set RGB color");
    DEBUG_PRINTLN("  /bulb/{name}/temperature/N   - Set temp (2000-9000K)");
    DEBUG_PRINTLN("  /bulb/{name}/connect         - Connect");
    DEBUG_PRINTLN("  /bulb/{name}/disconnect      - Disconnect");
}

void loop() {
    if(!setupComplete) return;
    
    unsigned long now = millis();
    static unsigned long lastDebug = 0;
    
    if(now - lastDebug >= 30000) {
        DEBUG_PRINT("\n[STATUS] Bulbs: ");
        for(int i = 0; i < numBulbs; i++) {
            DEBUG_PRINT(bulbs[i].name);
            DEBUG_PRINT("=");
            DEBUG_PRINT(bulbs[i].connected ? "OK" : "DOWN");
            if(i < numBulbs - 1) DEBUG_PRINT(", ");
        }
        DEBUG_PRINT(" | Heap: ");
        DEBUG_PRINT(ESP.getFreeHeap());
        DEBUG_PRINTLN(" bytes");
        lastDebug = now;
    }
    
    for(int i = 0; i < numBulbs; i++) {
        if(bulbs[i].connected && (now - bulbs[i].lastKeepAlive >= KEEPALIVE_INTERVAL)) {
            sendKeepAlive(i);
            bulbs[i].lastKeepAlive = now;
        }
    }
    
    handleSerialInput();
    
    delay(10);
}
