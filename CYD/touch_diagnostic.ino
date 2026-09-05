/*
 * Touchscreen Diagnostic for Hosyond 4" ESP32-3248S040 CYD
 * 
 * Draws crosshair targets at:
 *   - Top-Left, Top-Right, Bottom-Left, Bottom-Right corners
 *   - Center
 *
 * When you touch the screen it shows:
 *   - A dot where the library thinks you touched (mapped coords)
 *   - Raw ADC values from the touch controller
 *   - Mapped X/Y pixel coordinates
 *   - Which quadrant the library assigned the touch to
 *
 * Use this to compare where you physically touched vs where it registers.
 * Report back the raw values from each corner — that will tell us what
 * rotation/mirror correction is needed in the main receiver sketch.
 */

#include <TFT_eSPI.h>
#include <SPI.h>

TFT_eSPI tft = TFT_eSPI();

// Display size in current rotation
#define DISP_W 480
#define DISP_H 320

// Touch calibration from your calibration file
uint16_t calData[5] = { 305, 3590, 251, 3482, 7 };

// Target crosshair positions: label, x, y
struct Target {
  const char* label;
  int x;
  int y;
};

const int MARGIN = 30;
Target targets[] = {
  { "TL", MARGIN,          MARGIN          },
  { "TR", DISP_W - MARGIN, MARGIN          },
  { "BL", MARGIN,          DISP_H - MARGIN },
  { "BR", DISP_W - MARGIN, DISP_H - MARGIN },
  { "C",  DISP_W / 2,      DISP_H / 2      },
};
const int NUM_TARGETS = 5;

void drawCrosshair(int x, int y, uint16_t color, const char* label) {
  int arm = 12;
  int r   = 6;
  tft.drawLine(x - arm, y, x + arm, y, color);
  tft.drawLine(x, y - arm, x, y + arm, color);
  tft.drawCircle(x, y, r, color);

  int lx = x + (x < DISP_W / 2 ? 15 : -25);
  int ly = y + (y < DISP_H / 2 ? 10 : -18);
  tft.setTextColor(color, TFT_BLACK);
  tft.setTextSize(1);
  tft.setCursor(lx, ly);
  tft.print(label);
}

void drawAllTargets() {
  tft.fillScreen(TFT_BLACK);

  tft.drawLine(DISP_W / 2, 0, DISP_W / 2, DISP_H, tft.color565(40, 40, 40));
  tft.drawLine(0, DISP_H / 2, DISP_W, DISP_H / 2, tft.color565(40, 40, 40));

  for (int i = 0; i < NUM_TARGETS; i++) {
    drawCrosshair(targets[i].x, targets[i].y, TFT_YELLOW, targets[i].label);
  }

  tft.setTextColor(TFT_CYAN, TFT_BLACK);
  tft.setTextSize(1);
  tft.setCursor(4, DISP_H / 2 - 8);
  tft.print("Touch a target");
}

void drawTouchInfo(uint16_t tx, uint16_t ty, uint16_t raw_x, uint16_t raw_y) {
  tft.fillCircle(tx, ty, 5, TFT_RED);
  tft.drawCircle(tx, ty, 7, TFT_WHITE);

  int panel_y = DISP_H - 52;
  tft.fillRect(0, panel_y, DISP_W, 52, tft.color565(20, 20, 20));
  tft.drawRect(0, panel_y, DISP_W, 52, TFT_WHITE);

  tft.setTextColor(TFT_WHITE, tft.color565(20, 20, 20));
  tft.setTextSize(1);

  tft.setCursor(6, panel_y + 4);
  tft.printf("Mapped  X: %3d   Y: %3d", tx, ty);

  tft.setCursor(6, panel_y + 16);
  tft.printf("Raw     X: %4d  Y: %4d", raw_x, raw_y);

  const char* quad = "";
  if      (tx < DISP_W / 2 && ty < DISP_H / 2) quad = "TOP-LEFT";
  else if (tx >= DISP_W / 2 && ty < DISP_H / 2) quad = "TOP-RIGHT";
  else if (tx < DISP_W / 2 && ty >= DISP_H / 2) quad = "BOTTOM-LEFT";
  else                                            quad = "BOTTOM-RIGHT";

  tft.setCursor(6, panel_y + 28);
  tft.setTextColor(TFT_GREEN, tft.color565(20, 20, 20));
  tft.printf("Quadrant: %s", quad);

  tft.setCursor(6, panel_y + 40);
  tft.setTextColor(TFT_CYAN, tft.color565(20, 20, 20));
  tft.print("Tap corner again or wait 3s to reset");

  Serial.printf("Touch -> mapped(%d, %d)  raw(%d, %d)  quad=%s\n",
                tx, ty, raw_x, raw_y, quad);
}

void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n=== CYD Touch Diagnostic ===");

  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, HIGH);

  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(false);
  tft.setTouch(calData);

  drawAllTargets();
  Serial.println("Display ready. Touch the screen.");
}

unsigned long lastTouch = 0;

void loop() {
  uint16_t tx, ty;

  if (tft.getTouch(&tx, &ty)) {
    drawTouchInfo(tx, ty, 0, 0);
    lastTouch = millis();
  }

  if (lastTouch > 0 && millis() - lastTouch > 3000) {
    lastTouch = 0;
    drawAllTargets();
    Serial.println("--- Reset ---");
  }

  delay(30);
}