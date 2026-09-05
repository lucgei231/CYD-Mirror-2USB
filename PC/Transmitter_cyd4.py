#!/usr/bin/env python3
"""
Pixel Update Screenshot Sender — CYD2USB 2.8" CYD (ILI9341 240x320 portrait)

Captures a 600x800 region of a 1280x800 display (x=680..1279, y=0..799,
a 3:4 region matching the display's 240x320 aspect), scales it to
240x320, streams per-pixel updates to the ESP32. No cursor overlay.

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
import socket
import struct
import time
from typing import Optional

import cv2
import mss
import numpy as np

DEFAULT_IP   = "192.168.1.100" #Fallback IP if one isn't stated in '--ip <YOUR IP>' If you enter your device's IP here, then just type 'python Transmitter.py' to run the script. 
DEFAULT_PORT = 8090

# Source capture region — right 600px of a 1280x800 screen (3:4 matches 240x320)
CAPTURE_LEFT   = 680
CAPTURE_TOP    = 0
CAPTURE_WIDTH  = 600
CAPTURE_HEIGHT = 800

# Target display resolution (portrait 240x320)
DISPLAY_WIDTH  = 240
DISPLAY_HEIGHT = 320

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
    ) -> None:
        self.ip                    = ip
        self.port                  = port
        self.target_fps            = target_fps
        self.threshold             = threshold
        self.full_frame            = full_frame
        self.max_updates_per_frame = max_updates_per_frame

        self.sock: Optional[socket.socket] = None
        self.prev_rgb: Optional[np.ndarray] = None
        self.sent_initial_full: bool = False
        self.frame_id: int = 0
        self.sct: Optional[mss.mss] = None

        self.region = {
            "left":   CAPTURE_LEFT,
            "top":    CAPTURE_TOP,
            "width":  CAPTURE_WIDTH,
            "height": CAPTURE_HEIGHT,
        }

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
        print(
            f"[MON] Capturing right half of screen: "
            f"x={CAPTURE_LEFT}, y={CAPTURE_TOP}, "
            f"{CAPTURE_WIDTH}x{CAPTURE_HEIGHT}"
        )
        print(f"[MON] Scaling to {DISPLAY_WIDTH}x{DISPLAY_HEIGHT} (portrait)")
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
        frame   = np.ascontiguousarray(frame)
        resized = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT), interpolation=cv2.INTER_AREA)
        rgb     = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        rgb565  = self.rgb888_to_rgb565(rgb)
        return rgb, rgb565

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
        description="Stream a 600x800 screen region to CYD2USB 2.8\" ESP32 (240x320 portrait)"
    )
    parser.add_argument("--ip",                    type=str,   default=DEFAULT_IP,   help="ESP32 IP address")
    parser.add_argument("--port",                  type=int,   default=DEFAULT_PORT,  help="TCP port (default 8090)")
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
    )
    sender.run()


if __name__ == "__main__":
    main()
