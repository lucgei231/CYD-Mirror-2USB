# CYD-Mirror

- Made with Claude AI
- Mirror your Windows desktop to a 4" Cheap Yellow Display (ESP32) over WiFi.
- Optimized for ST7796 480x320 resolution with high-speed pixel diffing.

# ESP32 CYD Desktop Monitor

![Screenshot](docs/Screenshot.jpg)

### Stream a portion of your PC screen to a Hosyond 4" ESP32-3248S040 (CYD) over WiFi

This project adapts [tuckershannon's ESP32-Desktop-Monitor](https://github.com/tuckershannon/ESP32-Desktop-Monitor) for the **Hosyond 4" ESP32-3248S040 "Cheap Yellow Display" (CYD)** with an ST7796 480×320 touchscreen. The original project targeted a 135×240 ST7789 display — getting it working on the CYD required solving several hardware-specific challenges documented here.

---

## 🚀 Quick Start

### 1. PC Setup
* **[Python](https://www.python.org/downloads/):** Ensure you have Python 3.x installed.
* **[Arduino](https://www.arduino.cc/en/software/):** Install the [TFT_eSPI](https://github.com/Bodmer/TFT_eSPI) library.
* Run the [Python Script](https://github.com/CJM01/CYD-Mirror/blob/main/PC/Transmitter_cyd4.py)

### 2. CYD Configure & Flash (Arduino)
* Open `receiver_CYD4.ino` in the Arduino IDE.
* Update your WiFi credentials:
   ```cpp
   const char* ssid     = "YOUR_SSID";
   const char* password = "YOUR_PASSWORD";
* Flash to the CYD   

## Hardware

| Part | Details |
|---|---|
| **ESP32 Board** | [Hosyond 4" ESP32-3248S040 CYD](https://www.lcdwiki.com/4.0inch_ESP32-32E_Display) |
| **Display** | ST7796 480×320 (portrait: 320×480) Included with board |
| **Touch** | XPT2046 resistive touchscreen Included with board |
| **PC** | Windows, any resolution |

---

## What Was Changed From the Original

The original project used 1-byte (uint8) x/y coordinates, which worked fine for a 135×240 display but overflows on a 480×320 display. Every coordinate in the protocol had to be widened to uint16. Here's a summary of all changes:

### Protocol changes
The original protocol used 1-byte x and y coordinates. The CYD's 480px width exceeds 255, so all coordinates were widened to uint16:

| Packet | Original body format | New body format |
|---|---|---|
| PXUP pixel | `x(1) + y(1) + color(2)` = 4 bytes | `x(2) + y(2) + color(2)` = 6 bytes |
| PXUR run | `y(1) + x0(1) + len(1) + color(2)` = 5 bytes | `y(2) + x0(2) + len(2) + color(2)` = 8 bytes |

### Display driver changes
- Driver: ST7789 → **ST7796**
- Resolution: 135×240 → **480×320**
- Backlight: pin 4 → **pin 27**, active HIGH
- SPI frequency: 80MHz → **27MHz** (matches this board's User_Setup.h)
- Color order: BGR → **RGB** (ST7796 on this board)
- Mirror fix: added **MADCTL MX bit (0x40)** — `setRotation()` alone does not fix the horizontal mirror on this panel

### Python transmitter changes
- Removed all cursor overlay code (macOS only, not needed)
- Removed monitor selection logic — uses a hardcoded capture region instead
- Capture region: configurable crop of your screen (default: right half of 1280×800)
- Resize interpolation: uses `cv2.INTER_AREA` for better downscaling quality

---

## How the Mirror Fix Was Found

Trying all four `setRotation()` values (0–3) did not fix the horizontal mirror. The fix turned out to be in the **MADCTL register** — setting the MX bit (0x40) flips the display horizontally at the hardware level without affecting the touch calibration:

```cpp
tft.writecommand(TFT_MADCTL);
tft.writedata(TFT_MADCTL_RGB | 0x40);  // RGB + MX mirror fix
```

A touchscreen diagnostic sketch (included in this repo as `touch_diagnostic.ino`) was used to confirm the touch layer was correctly calibrated independently of the display orientation.

---

## Setup

### 1. Arduino IDE — `receiver_CYD4.ino`

**Required libraries:**
- `TFT_eSPI` by Bodmer (via Library Manager)

**`User_Setup.h`** (in your TFT_eSPI library folder):
```cpp
#define ST7796_DRIVER
#define TFT_MISO 12
#define TFT_MOSI 13
#define TFT_SCLK 14
#define TFT_CS   15
#define TFT_DC    2
#define TFT_RST  -1
#define TFT_BL   27
#define TFT_BACKLIGHT_ON HIGH
#define SPI_FREQUENCY  27000000
#define SPI_READ_FREQUENCY  20000000
#define SPI_TOUCH_FREQUENCY  2500000
```

**Steps:**
1. Open `receiver_CYD4.ino` in Arduino IDE
2. Set your WiFi credentials:
   ```cpp
   const char* ssid     = "YOUR_WIFI_SSID";
   const char* password = "YOUR_WIFI_PASSWORD";
   ```
3. Select board: **ESP32 Dev Module**
4. Flash to the CYD
5. Open Serial Monitor at **115200 baud** and note the IP address shown

### 2. Python — `transmitter_CYD4.py`

**Install dependencies:**
```powershell
pip install opencv-python mss numpy
```

**Run:**
```powershell
python transmitter_CYD4.py --ip <ESP32_IP>
```

**Capture region** (edit at top of script):
```python
CAPTURE_LEFT   = 640   # x start on your screen
CAPTURE_TOP    = 0     # y start on your screen
CAPTURE_WIDTH  = 640   # width of region to capture
CAPTURE_HEIGHT = 800   # height of region to capture
```

To capture the right half of a 1280×800 display: `LEFT=640, TOP=0, WIDTH=640, HEIGHT=800`  
To capture the full screen of a 1280×800 display: `LEFT=0, TOP=0, WIDTH=1280, HEIGHT=800`

---

## Command Line Options

```
--ip <IP>                    ESP32 IP address (required)
--port <PORT>                TCP port (default: 8090)
--target-fps <FPS>           Max frame rate (default: 15)
--threshold <N>              Pixel change sensitivity 0-255 (default: 5, higher = less sensitive)
--full-frame                 Send every pixel every frame (no diffing, slower)
--max-updates-per-frame <N>  Updates per packet (default: 3000)
```

---

## Performance Tuning

| Goal | Adjustment |
|---|---|
| Less bandwidth on static screens | Raise `--threshold` (e.g. 15) |
| Higher frame rate | Raise `--target-fps` (e.g. 20–25) |
| Better fast-motion | Raise `--max-updates-per-frame` (e.g. 6000) |
| Snappier display updates | Raise `SPI_TARGET_FREQ` in `.ino` to 40000000 |

---

## Troubleshooting

**Colors wrong** — try setting `useBgrSetting = true` in the `.ino`

**Image mirrored** — confirm `applyColorConfig()` includes `| 0x40` on the MADCTL writedata line

**Black area on one side** — your capture aspect ratio doesn't match the display. Adjust `CAPTURE_WIDTH` and `CAPTURE_HEIGHT` so their ratio matches your display orientation (3:2 for landscape 480×320, 2:3 for portrait 320×480)

**Low frame rate** — check WiFi signal, raise `--threshold`, lower `--target-fps`

**Connection drops** — ensure PC and ESP32 are on the same network, check firewall allows port 8090

---

## Files

| File | Description |
|---|---|
| `receiver_CYD4.ino` | ESP32 sketch for the CYD |
| `transmitter_CYD4.py` | Python screen capture sender for PC |
| `touch_diagnostic.ino` | Touchscreen calibration diagnostic sketch |
| `requirements.txt` | Python dependencies |

---

## Credits

- Original project: [tuckershannon/ESP32-Desktop-Monitor](https://github.com/tuckershannon/ESP32-Desktop-Monitor)
- TFT_eSPI library: [Bodmer/TFT_eSPI](https://github.com/Bodmer/TFT_eSPI)
- Adapted for the Hosyond 4" CYD (ESP32-3248S040) with ST7796 display
