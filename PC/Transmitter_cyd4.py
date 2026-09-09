#!/usr/bin/env python3
"""
Pixel Update Screenshot Sender — CYD2USB 2.8" CYD (ILI9341 320x240 landscape)

Captures a monitor (chosen with --display) and scales it to the 320x240
CYD display preserving aspect ratio. --mode 1 (default) fits the whole
screen with black bars; --mode 2 fills the display and shows only the
center of the screen. Draws the mouse cursor onto the streamed image.
Receives tap events from the CYD touchscreen and moves/clicks the mouse
accordingly (tap mapping follows the selected mode).

Protocol (little-endian):
  Pixel packet (PXUP):
    Header: 'PXUP' + version(1) + frame_id(4) + count(2) = 11 bytes
    Body:   count × [ x uint16(2) + y uint16(2) + color uint16(2) ] = 6 bytes each

  Run packet (PXUR):
    Header: 'PXUR' + version(1) + frame_id(4) + count(2) = 11 bytes
    Body:   count × [ y uint16(2) + x0 uint16(2) + length uint16(2) + color uint16(2) ] = 8 bytes each

Usage:
    python transmitter_CYD4.py --ip <ESP32_IP>
"""

import argparse
import ctypes
import select
import socket
import struct
import threading
import time
from typing import Optional

import cv2
import mss
import numpy as np

# Windows mouse control via ctypes (no extra dependencies)
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

try:
    _user32 = ctypes.windll.user32
except (AttributeError, OSError):
    _user32 = None

if _user32 is not None:
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.mouse_event.argtypes = [ctypes.c_uint] * 5


def get_cursor_pos():
    if _user32 is None:
        return None
    pt = _POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor_pos(x, y):
    if _user32 is None:
        return
    _user32.SetCursorPos(int(x), int(y))


def left_click():
    if _user32 is None:
        return
    _user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    _user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


def draw_cursor(canvas, x, y):
    # Small arrow pointer, white with a thin black outline (canvas is BGR).
    # The tip (x, y) is the exact cursor position.
    pts = np.array(
        [
            [x, y],
            [x, y + 11],
            [x + 3, y + 8],
            [x + 5, y + 12],
            [x + 7, y + 11],
            [x + 4, y + 7],
            [x + 7, y + 6],
        ],
        dtype=np.int32,
    )
    cv2.polylines(canvas, [pts], True, (0, 0, 0), 1)
    cv2.fillPoly(canvas, [pts], (255, 255, 255))

DEFAULT_IP   = "192.168.1.100" #Fallback IP if one isn't stated in '--ip <YOUR IP>' If you enter your device's IP here, then just type 'python Transmitter.py' to run the script. 
DEFAULT_PORT = 8090

# Target display resolution (landscape 320x240)
DISPLAY_WIDTH  = 320
DISPLAY_HEIGHT = 240

# Touch event packet (sent CYD -> PC): 'TOUC' + version(1) + state(1) + x(2) + y(2)
# state: 1 = pressed, 2 = moved while pressed, 0 = released
TOUCH_MAGIC  = b"TOUC"
TOUCH_PACKET = 10

# Button packet (sent CYD -> PC): 'BUTN' + version(1) + state(1) + pad(2)
BUTTON_MAGIC  = b"BUTN"
BUTTON_PACKET = 8

HEADER_VERSION     = 0x02
RUN_HEADER_VERSION = 0x01


class ScreenshotPixelSender:
    def __init__(
        self,
        ip: str,
        port: int,
        target_fps: float,
        threshold: int,
        full_frame: bool,
        max_updates_per_frame: int,
        display: int = 1,
        mode: int = 1,
        zoom_enabled: bool = True,
    ) -> None:
        self.ip                    = ip
        self.port                  = port
        self.target_fps            = target_fps
        self.threshold             = threshold
        self.full_frame            = full_frame
        self.max_updates_per_frame = max_updates_per_frame
        self.display               = display
        self.mode                  = mode
        self.zoom_enabled          = zoom_enabled

        self.sock: Optional[socket.socket] = None
        self.prev_rgb: Optional[np.ndarray] = None
        self.sent_initial_full: bool = False
        self.frame_id: int = 0
        self.sct: Optional[mss.mss] = None
        self.recv_buf: bytes = b""

        self.region = None  # set from the mss monitor list in setup_capture

        # Letterbox fit parameters (set on each frame, used to map taps back)
        self.fit_scale = 1.0
        self.fit_x0 = 0
        self.fit_y0 = 0

        # Pan/zoom view state (monitor-local pixels)
        self.base_vw = DISPLAY_WIDTH
        self.base_vh = DISPLAY_HEIGHT
        self.view_active = False
        self.view_zoom = 1.0
        self.view_cx = 0.0
        self.view_cy = 0.0

        # Gesture state: hold BOOT + drag = pan, double-tap = zoom, tap = click
        self.btn_held = False
        self.touch_gesture = None  # None | "tap" | "pan" | "dead"
        self.touch_start = (0, 0)
        self.touch_last = (0, 0)
        self.pending_click = None  # deferred single-tap click (Timer)
        self.last_tap_time = 0.0
        self.last_tap_disp = (0, 0)  # previous tap position in DISPLAY pixels
        self.pan_logged = False

    # ------------------------------------------------------------------ connection
    def ensure_connection(self) -> bool:
        if self.sock:
            return True
        return self.connect()

    def connect(self, retries: int = 3) -> bool:
        for attempt in range(1, retries + 1):
            try:
                if self.sock:
                    self.sock.close()
                print(f"[CONNECT] Attempt {attempt}/{retries} to {self.ip}:{self.port}")
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.settimeout(10)
                self.sock.connect((self.ip, self.port))
                print("[CONNECT] ✓ Connected")
                return True
            except Exception as exc:
                print(f"[CONNECT] ✗ {type(exc).__name__}: {exc}")
                if attempt < retries:
                    time.sleep(2)
        return False

    def disconnect(self) -> None:
        if self.sock:
            self.sock.close()
        self.sock = None
        print("[CONNECT] Disconnected")

    # ------------------------------------------------------------------ capture
    def setup_capture(self) -> bool:
        try:
            self.sct = mss.mss()
        except Exception as exc:
            print(f"[MON] Unable to start screen capture: {exc}")
            return False
        if self.display < 1 or self.display >= len(self.sct.monitors):
            print(f"[MON] Display {self.display} not found. Available monitors:")
            for i, m in enumerate(self.sct.monitors[1:], start=1):
                print(f"  {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})")
            return False
        mon = self.sct.monitors[self.display]
        self.region = {
            "left":   mon["left"],
            "top":    mon["top"],
            "width":  mon["width"],
            "height": mon["height"],
        }
        print(
            f"[MON] Capturing monitor {self.display}: "
            f"{self.region['width']}x{self.region['height']}"
        )
        if self.mode == 1:
            print(f"[MON] Mode 1: scaling to fit {DISPLAY_WIDTH}x{DISPLAY_HEIGHT} (black bars)")
        else:
            print(f"[MON] Mode 2: filling {DISPLAY_WIDTH}x{DISPLAY_HEIGHT} (center crop)")

        # Base 4:3 view window: fit (mode 1) or cover (mode 2) of the monitor
        fw, fh = self.region["width"], self.region["height"]
        if self.mode == 1:
            self.base_vw = max(8, min(fw, int(fh * 4 / 3)))
        else:  # mode 2: cover
            base_vh = max(6, min(fh, int(fw * 3 / 4)))
            self.base_vw = max(8, int(base_vh * 4 / 3))
        self.base_vh = max(6, int(self.base_vw * 3 / 4))
        self.view_cx = fw / 2.0
        self.view_cy = fh / 2.0
        self.view_active = (self.mode == 2)
        return True

    def grab_frame(self) -> Optional[np.ndarray]:
        if not self.sct:
            return None
        try:
            shot = self.sct.grab(self.region)
        except Exception as exc:
            print(f"[MON] Capture failed: {exc}")
            return None
        # mss returns BGRA; drop alpha → BGR
        return np.array(shot)[:, :, :3]

    # ------------------------------------------------------------------ conversion
    @staticmethod
    def rgb888_to_rgb565(rgb: np.ndarray) -> np.ndarray:
        r = (rgb[:, :, 0] >> 3).astype(np.uint16)
        g = (rgb[:, :, 1] >> 2).astype(np.uint16)
        b = (rgb[:, :, 2] >> 3).astype(np.uint16)
        return (r << 11) | (g << 5) | b

    def resize_and_convert(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        frame = np.ascontiguousarray(frame)
        fh, fw = frame.shape[:2]
        canvas = np.zeros((DISPLAY_HEIGHT, DISPLAY_WIDTH, 3), dtype=np.uint8)

        if self.view_active:
            # Windowed view: a 4:3 crop of the monitor (zoomed/panned) fills the
            # display edge-to-edge — pan (BOOT + drag) and zoom (double-tap)
            # move/resize this window
            vw = min(fw, max(8, int(round(self.base_vw / self.view_zoom))))
            vh = min(fh, max(6, int(round(vw * 3 / 4))))
            cx = min(max(self.view_cx, vw / 2.0), fw - vw / 2.0)
            cy = min(max(self.view_cy, vh / 2.0), fh - vh / 2.0)
            left = int(cx - vw / 2.0)
            top = int(cy - vh / 2.0)
            canvas = cv2.resize(
                frame[top:top + vh, left:left + vw],
                (DISPLAY_WIDTH, DISPLAY_HEIGHT),
                interpolation=cv2.INTER_AREA,
            )
            # Fit params in display-px-per-monitor-px convention (same as the
            # letterbox branch): display_x = fit_x0 + monitor_local_x * fit_scale
            self.fit_scale = DISPLAY_WIDTH / vw
            self.fit_x0 = -left * self.fit_scale
            self.fit_y0 = -top * self.fit_scale
        else:
            # Mode 1 at rest (no gesture yet): whole monitor fitted with black bars
            scale  = min(DISPLAY_WIDTH / fw, DISPLAY_HEIGHT / fh)
            scaled = cv2.resize(
                frame,
                (max(1, int(round(fw * scale))), max(1, int(round(fh * scale)))),
                interpolation=cv2.INTER_AREA,
            )
            x0 = (DISPLAY_WIDTH  - scaled.shape[1]) // 2
            y0 = (DISPLAY_HEIGHT - scaled.shape[0]) // 2
            canvas[y0:y0 + scaled.shape[0], x0:x0 + scaled.shape[1]] = scaled
            self.fit_scale = scale
            self.fit_x0 = x0
            self.fit_y0 = y0

        # Overlay the mouse cursor (only when it is inside the captured monitor).
        # Display pos = monitor-local pos * scale + fit offset (both modes).
        cur = get_cursor_pos()
        if cur is not None and self.region is not None:
            lx = cur[0] - self.region["left"]
            ly = cur[1] - self.region["top"]
            if 0 <= lx < self.region["width"] and 0 <= ly < self.region["height"]:
                cx = self.fit_x0 + int(lx * self.fit_scale)
                cy = self.fit_y0 + int(ly * self.fit_scale)
                if 0 <= cx < DISPLAY_WIDTH - 8 and 0 <= cy < DISPLAY_HEIGHT - 13:
                    draw_cursor(canvas, cx, cy)

        rgb    = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        rgb565 = self.rgb888_to_rgb565(rgb)
        return rgb, rgb565

    # ------------------------------------------------------------------ touch
    def _cancel_pending_click(self) -> None:
        if self.pending_click is not None:
            self.pending_click.cancel()
            self.pending_click = None

    def _do_click(self, sx: int, sy: int) -> None:
        self.pending_click = None
        left_click()

    def _toggle_zoom(self, x: int, y: int) -> None:
        old_zoom = self.view_zoom
        self.view_zoom = 1.0 if old_zoom > 1.0 else 2.0
        self.view_active = True
        # Anchor: keep the monitor point under the tap stationary while zooming.
        # Fractions are across the *content area* currently shown on the
        # display — in mode 1 at rest that area is inset by the letterbox bars.
        old_vw = self.base_vw / old_zoom
        old_vh = self.base_vh / old_zoom
        new_vw = self.base_vw / self.view_zoom
        new_vh = self.base_vh / self.view_zoom
        # Fraction of the monitor under the tap (valid in both render modes)
        mx = (x - self.fit_x0) / self.fit_scale
        my = (y - self.fit_y0) / self.fit_scale
        ax = min(1.0, max(0.0, mx / self.region["width"]))
        ay = min(1.0, max(0.0, my / self.region["height"]))
        self.view_cx += (old_vw - new_vw) * (ax - 0.5)
        self.view_cy += (old_vh - new_vh) * (ay - 0.5)
        print(
            f"[ZOOM] {'out' if old_zoom > 1.0 else 'in'} -> {self.view_zoom:.1f}x "
            f"(window {new_vw:.0f}x{new_vh:.0f} monitor px)"
        )

    def handle_touch(self, state: int, x: int, y: int) -> None:
        if not self.region or self.fit_scale <= 0:
            return
        # Display coords -> monitor-local coords (inverse of the current view)
        lx = (x - self.fit_x0) / self.fit_scale
        ly = (y - self.fit_y0) / self.fit_scale
        left, top = self.region["left"], self.region["top"]
        right = left + self.region["width"] - 1
        bottom = top + self.region["height"] - 1
        sx = int(max(left, min(right, left + lx)))
        sy = int(max(top, min(bottom, top + ly)))

        if not self.zoom_enabled:
            # Legacy behavior: tap moves the cursor on touch-down and
            # left-clicks immediately on release (no zoom/pan/deferral)
            if state == 1:
                set_cursor_pos(sx, sy)
            elif state == 0:
                left_click()
            return

        if state == 1:  # finger down
            self._cancel_pending_click()
            self.touch_start = (x, y)
            self.touch_last = (x, y)
            self.pan_logged = False
            if self.btn_held:
                self.touch_gesture = "pan"     # BOOT held -> pan gesture
            else:
                self.touch_gesture = "tap"
                set_cursor_pos(sx, sy)
            print(f"[TOUCH] down -> screen({sx}, {sy}) gesture={self.touch_gesture}")
            return

        if state == 2:  # finger moved
            if self.touch_gesture is None:
                return
            dx = x - self.touch_last[0]
            dy = y - self.touch_last[1]
            self.touch_last = (x, y)
            if self.touch_gesture == "pan":
                if not self.pan_logged:
                    self.pan_logged = True
                    print("[TOUCH] pan move (view follows)")
                self.view_cx -= dx / self.fit_scale   # display px -> monitor px
                self.view_cy -= dy / self.fit_scale
                self.view_active = True
            elif self.touch_gesture == "tap":
                moved = abs(x - self.touch_start[0]) + abs(y - self.touch_start[1])
                if moved > 6:
                    self.touch_gesture = "dead"  # sloppy tap: no click, no pan
            return

        # state == 0: finger up
        gesture = self.touch_gesture
        self.touch_gesture = None
        moved_total = abs(x - self.touch_start[0]) + abs(y - self.touch_start[1])
        if gesture == "dead" and moved_total < 12:
            gesture = "tap"  # tiny jitter — still counts as a tap

        if gesture == "tap":
            now = time.time()
            # Distance measured in DISPLAY pixels so the double-tap tolerance
            # does not depend on the current zoom level
            tap_dist = abs(x - self.last_tap_disp[0]) + abs(y - self.last_tap_disp[1])
            if self.last_tap_time and now - self.last_tap_time < 0.4 and tap_dist < 40:
                # Double-tap: cancel the pending click and toggle zoom instead
                self.last_tap_time = 0.0
                self._toggle_zoom(x, y)
            else:
                # Single tap (so far): defer the click in case a second tap follows
                self.last_tap_time = now
                self.last_tap_disp = (x, y)
                self.pending_click = threading.Timer(0.4, self._do_click, args=(sx, sy))
                self.pending_click.daemon = True
                self.pending_click.start()
        else:
            self.last_tap_time = 0.0
        print(f"[TOUCH] up -> screen({sx}, {sy})")

    def process_device_packets(self) -> None:
        if not self.sock:
            return
        while True:
            ready, _, _ = select.select([self.sock], [], [], 0)
            if not ready:
                break
            try:
                data = self.sock.recv(4096)
            except Exception:
                break
            if not data:
                self.disconnect()
                break
            self.recv_buf += data
            while len(self.recv_buf) >= 4:
                if self.recv_buf[:4] == TOUCH_MAGIC:
                    if len(self.recv_buf) < TOUCH_PACKET:
                        break
                    state = self.recv_buf[5]
                    x = self.recv_buf[6] | (self.recv_buf[7] << 8)
                    y = self.recv_buf[8] | (self.recv_buf[9] << 8)
                    self.recv_buf = self.recv_buf[TOUCH_PACKET:]
                    self.handle_touch(state, x, y)
                elif self.recv_buf[:4] == BUTTON_MAGIC:
                    if len(self.recv_buf) < BUTTON_PACKET:
                        break
                    state = self.recv_buf[5]
                    self.recv_buf = self.recv_buf[BUTTON_PACKET:]
                    if not self.zoom_enabled:
                        continue
                    if state == 1:
                        self.btn_held = True
                        # If a finger is already down, this hold becomes a pan
                        if self.touch_gesture == "tap":
                            self.touch_gesture = "pan"
                    else:
                        self.btn_held = False
                else:
                    self.recv_buf = self.recv_buf[1:]  # resync

    # ------------------------------------------------------------------ packets
    def build_packets(self, rgb: np.ndarray, rgb565: np.ndarray) -> list[bytes]:
        if self.full_frame or not self.sent_initial_full or self.prev_rgb is None:
            mask = np.ones((DISPLAY_HEIGHT, DISPLAY_WIDTH), dtype=bool)
        else:
            diff = np.abs(rgb.astype(np.int16) - self.prev_rgb.astype(np.int16))
            mask = diff.max(axis=2) > self.threshold

        ys, xs = np.nonzero(mask)
        colors = rgb565[ys, xs]
        count  = len(colors)

        if count == 0:
            self.frame_id += 1
            return [
                b"PXUP"
                + bytes([HEADER_VERSION])
                + struct.pack("<IH", self.frame_id, 0)
            ]

        run_packets   = self._build_run_packets(mask, rgb565)
        pixel_packets = self._build_pixel_packets(xs, ys, colors, count)

        if sum(len(p) for p in run_packets) < sum(len(p) for p in pixel_packets):
            self.frame_id += len(run_packets)
            return run_packets
        self.frame_id += len(pixel_packets)
        return pixel_packets

    def _build_pixel_packets(
        self, xs: np.ndarray, ys: np.ndarray, colors: np.ndarray, count: int
    ) -> list[bytes]:
        # Pixel entry: x uint16 + y uint16 + color uint16 = 6 bytes
        packets: list[bytes] = []
        max_per = max(1, self.max_updates_per_frame)
        start = 0
        while start < count:
            end    = min(start + max_per, count)
            n      = end - start
            header = b"PXUP" + bytes([HEADER_VERSION]) + struct.pack("<IH", self.frame_id, n)
            payload = bytearray(header)
            for x, y, color in zip(xs[start:end], ys[start:end], colors[start:end]):
                payload.extend(struct.pack("<HHH", int(x), int(y), int(color)))
            packets.append(bytes(payload))
            start = end
        return packets

    def _build_run_packets(self, mask: np.ndarray, rgb565: np.ndarray) -> list[bytes]:
        # Run entry: y uint16 + x0 uint16 + length uint16 + color uint16 = 8 bytes
        packets: list[bytes] = []
        max_per = max(1, self.max_updates_per_frame)
        runs: list[tuple[int, int, int, int]] = []  # y, x0, length, color

        for y in range(DISPLAY_HEIGHT):
            row_mask = mask[y]
            if not row_mask.any():
                continue
            x = 0
            while x < DISPLAY_WIDTH:
                if not row_mask[x]:
                    x += 1
                    continue
                x0    = x
                color = int(rgb565[y, x0])
                x += 1
                while x < DISPLAY_WIDTH and row_mask[x] and int(rgb565[y, x]) == color:
                    x += 1
                runs.append((y, x0, x - x0, color))

        total_runs = len(runs)
        if total_runs == 0:
            return [b"PXUP" + bytes([HEADER_VERSION]) + struct.pack("<IH", self.frame_id, 0)]

        start = 0
        while start < total_runs:
            end    = min(start + max_per, total_runs)
            n      = end - start
            header = b"PXUR" + bytes([RUN_HEADER_VERSION]) + struct.pack("<IH", self.frame_id, n)
            payload = bytearray(header)
            for y, x0, length, color in runs[start:end]:
                payload.extend(struct.pack("<HHHH", int(y), int(x0), int(length), int(color)))
            packets.append(bytes(payload))
            start = end
        return packets

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        if not self.setup_capture():
            return
        if not self.ensure_connection():
            return

        frame_delay = 1.0 / self.target_fps if self.target_fps > 0 else 0.0
        frame_count = sent_packets = sent_pixels = 0
        start_t = time.time()

        print("[STREAM] Starting (Ctrl+C to stop)")
        try:
            while True:
                frame_start = time.time()
                frame = self.grab_frame()
                if frame is None:
                    print("[STREAM] Capture stopped")
                    break

                rgb, rgb565 = self.resize_and_convert(frame)
                packets = self.build_packets(rgb, rgb565)
                self.prev_rgb = rgb

                # Handle taps / button presses arriving from the CYD
                self.process_device_packets()

                if not self.ensure_connection():
                    print("[SEND] Could not reconnect; exiting")
                    break

                for pkt in packets:
                    updates_in_frame = struct.unpack_from("<H", pkt, 9)[0]
                    try:
                        self.sock.sendall(pkt)
                        sent_packets += 1
                        sent_pixels  += updates_in_frame
                        if not self.sent_initial_full:
                            self.sent_initial_full = True
                    except (BrokenPipeError, ConnectionResetError):
                        print("[SEND] Connection lost; attempting reconnect")
                        self.disconnect()
                        if not self.ensure_connection():
                            print("[SEND] Reconnect failed; exiting")
                            break
                        try:
                            self.sock.sendall(pkt)
                            sent_packets += 1
                            sent_pixels  += updates_in_frame
                        except Exception as exc:
                            print(f"[SEND] Retry failed: {exc}")
                            break
                    except Exception as exc:
                        print(f"[SEND] Error: {exc}")
                        self.disconnect()
                        break
                else:
                    frame_count += 1
                    now = time.time()
                    elapsed = now - frame_start
                    if frame_delay > 0 and elapsed < frame_delay:
                        time.sleep(frame_delay - elapsed)
                    if now - start_t >= 1.0:
                        fps = frame_count / (now - start_t)
                        print(
                            f"[STATS] frames:{frame_count} packets:{sent_packets} "
                            f"pixels:{sent_pixels} fps~{fps:.2f}"
                        )
                        start_t     = now
                        frame_count = sent_packets = sent_pixels = 0
                    continue
                break
        except KeyboardInterrupt:
            print("\n[STREAM] Interrupted by user")
        finally:
            self.disconnect()
            if self.sct:
                self.sct.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Stream the whole screen to CYD2USB 2.8\" ESP32 (320x240 landscape, letterboxed)"
    )
    parser.add_argument("--ip",                    type=str,   default=DEFAULT_IP,   help="ESP32 IP address")
    parser.add_argument("--port",                  type=int,   default=DEFAULT_PORT,  help="TCP port (default 8090)")
    parser.add_argument("--display",               type=int,   default=1,            help="Monitor to capture: 1 = primary, 2 = second, ... (default 1)")
    parser.add_argument("--mode",                  type=int,   choices=[1, 2], default=1, help="Scaling mode: 1 = fit with black bars (default), 2 = fill screen showing only the center")
    parser.add_argument("--zoom",                  type=str,   choices=["true", "false"], default="true", help="Enable zoom (double-tap) and pan (BOOT+drag): 'true' or 'false' (default true)")
    parser.add_argument("--target-fps",            type=float, default=15.0,          help="Max frame rate")
    parser.add_argument("--threshold",             type=int,   default=5,             help="Pixel-change threshold 0-255")
    parser.add_argument("--full-frame",            action="store_true",               help="Send every pixel every frame")
    parser.add_argument("--max-updates-per-frame", type=int,   default=3000,          help="Updates per packet")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    sender = ScreenshotPixelSender(
        ip=args.ip,
        port=args.port,
        target_fps=args.target_fps,
        threshold=args.threshold,
        full_frame=args.full_frame,
        max_updates_per_frame=args.max_updates_per_frame,
        display=args.display,
        mode=args.mode,
        zoom_enabled=args.zoom.lower() == "true",
    )
    sender.run()


if __name__ == "__main__":
    main()
