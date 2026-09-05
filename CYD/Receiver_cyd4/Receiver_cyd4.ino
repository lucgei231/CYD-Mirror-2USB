/*
 * Pixel Update Receiver for CYD2USB 2.8" ESP32-2432S028R (ILI9341 240x320 portrait)
 *
 * Protocol (little-endian):
 *
 * Pixel packet (PXUP):
 *   Header: 'P','X','U','P' (4) + version 0x02 (1) + frame_id uint32 (4) + count uint16 (2) = 11 bytes
 *   Body:   count × [ x uint16 (2) + y uint16 (2) + color uint16 (2) ] = 6 bytes each
 *
 * Run packet (PXUR):
 *   Header: 'P','X','U','R' (4) + version 0x01 (1) + frame_id uint32 (4) + count uint16 (2) = 11 bytes
 *   Body:   count × [ y uint16 (2) + x0 uint16 (2) + length uint16 (2) + color uint16 (2) ] = 8 bytes each
 */

#include <TFT_eSPI.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiServer.h>
#include <esp_heap_caps.h>

#define TFT_MADCTL     0x36
#define TFT_MADCTL_RGB 0x00
#define TFT_MADCTL_BGR 0x08

TFT_eSPI tft = TFT_eSPI();

#define DISPLAY_WIDTH  240
#define DISPLAY_HEIGHT 320

#ifndef TFT_BL
#define TFT_BL 21
#endif

const uint32_t SPI_TARGET_FREQ = 27000000;

// *** UPDATE THESE ***
const char* ssid     = "VM6842809";
const char* password = "h2hFnrycpprn";

WiFiServer server(8090);
WiFiClient client;

const uint8_t MAGIC[4]       = {'P', 'X', 'U', 'P'};
const uint8_t PROTO_VERSION   = 0x02;
const size_t  HEADER_SIZE     = 11;
const uint8_t MAGIC_RUN[4]   = {'P', 'X', 'U', 'R'};
const uint8_t RUN_VERSION     = 0x01;
const size_t  RUN_HEADER_SIZE = 11;

bool swapBytesSetting = false;
bool useBgrSetting    = true;  // CYD2USB 2.8" ILI9341 panel is BGR

unsigned long frameCount     = 0;
unsigned long lastStats      = 0;
unsigned long updatesApplied = 0;
uint32_t      lastFrameId    = 0;

struct PixelUpdate {
  uint16_t x;
  uint16_t y;
  uint16_t len;
  uint16_t color;
};

PixelUpdate* updateBuffer   = nullptr;
uint32_t     bufferCapacity = 0;
bool         dmaEnabled     = false;

bool ensureUpdateBuffer(uint32_t needed) {
  if (needed <= bufferCapacity && updateBuffer != nullptr) return true;
  PixelUpdate* tmp = (PixelUpdate*)ps_malloc(needed * sizeof(PixelUpdate));
  if (!tmp) tmp = (PixelUpdate*)malloc(needed * sizeof(PixelUpdate));
  if (!tmp) { Serial.println("Failed to allocate update buffer"); return false; }
  if (updateBuffer) free(updateBuffer);
  updateBuffer   = tmp;
  bufferCapacity = needed;
  return true;
}

bool readExactly(WiFiClient& c, uint8_t* dst, size_t len) {
  size_t got = 0;
  while (got < len && c.connected()) {
    int chunk = c.read(dst + got, len - got);
    if (chunk > 0) got += chunk;
    else delay(1);
  }
  return got == len;
}

void applyColorConfig() {
  tft.setSwapBytes(swapBytesSetting);
  tft.writecommand(TFT_MADCTL);
  tft.writedata((useBgrSetting ? TFT_MADCTL_BGR : TFT_MADCTL_RGB) | 0x40);
}

void showWaitingScreen() {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.setCursor(10, 20);
  tft.println("Pixel RX - CYD 2.8\"");
  tft.setTextSize(1);
  tft.setCursor(10, 55);
  tft.println("IP Address:");
  tft.setTextSize(2);
  tft.setTextColor(TFT_GREEN, TFT_BLACK);
  tft.setCursor(10, 72);
  tft.println(WiFi.localIP().toString());
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(1);
  tft.setCursor(10, 105);
  tft.println("Waiting for connection...");
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== Pixel Update Receiver (CYD 2.8\") ===");

  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, HIGH);

  tft.init();
  SPI.setFrequency(SPI_TARGET_FREQ);
  dmaEnabled = tft.initDMA();
  tft.setRotation(0);
  applyColorConfig();
  tft.fillScreen(TFT_BLACK);

  Serial.print("Connecting to WiFi: ");
  Serial.println(ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(250);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nWiFi connection failed");
    tft.fillScreen(TFT_RED);
    tft.setTextColor(TFT_WHITE, TFT_RED);
    tft.setCursor(10, 50);
    tft.setTextSize(2);
    tft.println("WiFi FAILED!");
    while (true) delay(1000);
  }

  Serial.println("\nWiFi connected");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());

  showWaitingScreen();
  server.begin();
  server.setNoDelay(true);
  Serial.println("Server listening on port 8090");
}

bool handleClient() {
  if (!client || !client.connected()) {
    client = server.available();
    if (client) {
      Serial.println("Client connected");
      client.setNoDelay(true);
      client.setTimeout(50);
      frameCount     = 0;
      updatesApplied = 0;
      tft.fillScreen(TFT_BLACK);
    }
  }

  if (!client || !client.connected()) return false;
  if (client.available() < 11) return true;

  uint8_t magicBuf[4];
  if (!readExactly(client, magicBuf, 4)) { client.stop(); return false; }
  bool isRun   = (memcmp(magicBuf, MAGIC_RUN, 4) == 0);
  bool isPixel = (memcmp(magicBuf, MAGIC,     4) == 0);

  if (!isRun && !isPixel) {
    Serial.println("Bad magic; flushing stream");
    client.stop();
    return false;
  }

  // ---- Pixel packet (PXUP) ----
  // Body: count × [ x(2) + y(2) + color(2) ] = 6 bytes per entry
  if (isPixel) {
    uint8_t rest[HEADER_SIZE - 4];
    if (!readExactly(client, rest, sizeof(rest))) { client.stop(); return false; }
    if (rest[0] != PROTO_VERSION) {
      Serial.printf("Unsupported pixel version: 0x%02X\n", rest[0]);
      client.stop(); return false;
    }

    uint32_t frameId = (uint32_t)rest[1] | ((uint32_t)rest[2]<<8) | ((uint32_t)rest[3]<<16) | ((uint32_t)rest[4]<<24);
    uint16_t count   = rest[5] | (rest[6] << 8);

    if (count == 0) { frameCount++; lastFrameId = frameId; return true; }
    if (count > (DISPLAY_WIDTH * DISPLAY_HEIGHT)) {
      Serial.printf("Pixel count too large: %u\n", count);
      client.stop(); return false;
    }
    if (!ensureUpdateBuffer(count)) { client.stop(); return false; }

    uint8_t entry[6];  // x(2) + y(2) + color(2)
    for (uint16_t i = 0; i < count; i++) {
      if (!readExactly(client, entry, 6)) { client.stop(); return false; }
      updateBuffer[i].x     = entry[0] | (entry[1] << 8);
      updateBuffer[i].y     = entry[2] | (entry[3] << 8);
      updateBuffer[i].color = entry[4] | (entry[5] << 8);
    }

    tft.startWrite();
    for (uint16_t i = 0; i < count; i++) {
      uint16_t x = updateBuffer[i].x;
      uint16_t y = updateBuffer[i].y;
      if (x < DISPLAY_WIDTH && y < DISPLAY_HEIGHT) {
        tft.setAddrWindow(x, y, 1, 1);
        tft.writeColor(updateBuffer[i].color, 1);
        updatesApplied++;
      }
    }
    tft.endWrite();

    frameCount++; lastFrameId = frameId;
    unsigned long now = millis();
    if (now - lastStats > 2000) {
      Serial.printf("Frames: %lu (last id %u) | Updates: %lu\n", frameCount, lastFrameId, updatesApplied);
      lastStats = now;
    }
    return true;
  }

  // ---- Run packet (PXUR) ----
  // Body: count × [ y(2) + x0(2) + length(2) + color(2) ] = 8 bytes per entry
  uint8_t rest[RUN_HEADER_SIZE - 4];
  if (!readExactly(client, rest, sizeof(rest))) { client.stop(); return false; }
  if (rest[0] != RUN_VERSION) {
    Serial.printf("Unsupported run version: 0x%02X\n", rest[0]);
    client.stop(); return false;
  }

  uint32_t frameId = (uint32_t)rest[1] | ((uint32_t)rest[2]<<8) | ((uint32_t)rest[3]<<16) | ((uint32_t)rest[4]<<24);
  uint16_t count   = rest[5] | (rest[6] << 8);

  if (count == 0) { frameCount++; lastFrameId = frameId; return true; }
  if (count > (DISPLAY_WIDTH * DISPLAY_HEIGHT)) {
    Serial.printf("Run count too large: %u\n", count);
    client.stop(); return false;
  }
  if (!ensureUpdateBuffer(count)) { client.stop(); return false; }

  uint8_t entry[8];  // y(2) + x0(2) + length(2) + color(2)
  for (uint16_t i = 0; i < count; i++) {
    if (!readExactly(client, entry, 8)) { client.stop(); return false; }
    updateBuffer[i].y     = entry[0] | (entry[1] << 8);
    updateBuffer[i].x     = entry[2] | (entry[3] << 8);
    updateBuffer[i].len   = entry[4] | (entry[5] << 8);
    updateBuffer[i].color = entry[6] | (entry[7] << 8);
  }

  tft.startWrite();
  for (uint16_t i = 0; i < count; i++) {
    uint16_t x0     = updateBuffer[i].x;
    uint16_t y      = updateBuffer[i].y;
    uint16_t runLen = updateBuffer[i].len;
    if (x0 < DISPLAY_WIDTH && y < DISPLAY_HEIGHT && runLen > 0 && (x0 + runLen) <= DISPLAY_WIDTH) {
      tft.setAddrWindow(x0, y, runLen, 1);
      if (dmaEnabled) tft.pushBlock(updateBuffer[i].color, runLen);
      else            tft.writeColor(updateBuffer[i].color, runLen);
      updatesApplied += runLen;
    }
  }
  tft.endWrite();

  frameCount++; lastFrameId = frameId;
  unsigned long now = millis();
  if (now - lastStats > 2000) {
    Serial.printf("Frames: %lu (last id %u) | Updates: %lu\n", frameCount, lastFrameId, updatesApplied);
    lastStats = now;
  }
  return true;
}

void loop() {
  handleClient();
  if (client && !client.connected()) {
    Serial.println("Client disconnected");
    showWaitingScreen();
  }
  delay(1);
}
