#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simmy.py

macOS-focused UI recorder/replayer for iOS Simulator QA automation.

Key features:
- Multi-resolution image capture around interactions (micro/small/medium/large/context)
- Optional OCR text capture if pytesseract is installed
- ORB/template/ocr-based matching with intelligent retries and fallback to coordinate mapping
- UI state hashing to validate action effects
- Uses macOS native tools:
  - xcrun simctl to create/boot simulator, install app if needed, launch app
  - AppleScript to activate and place Simulator window
- Emergency abort hotkey: Ctrl+Shift+A+F+K (global)

Dependencies (no API keys/registration):
  pip install pillow mss pynput pyautogui numpy opencv-python pytesseract

Recording:
  python3 simmy.py --record --sim "iPhone 15" --name my_flow

Replay:
  python3 simmy.py --replay recordings/my_flow --sim "iPhone 15" \
    --bundle-id com.example.app --app-path /path/to/MyApp.app --debug

Notes:
- Replay opens a new Simulator instance for the specified device even if one is already open.
- Mouse cursor will move/click during replay (expected for GUI automation).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from typing import Any, Dict, List, Optional, Tuple

# -------- Defaults (override by CLI) --------
DEFAULT_BUNDLE_ID = "com.example.app"  # override via --bundle-id
DEFAULT_SIMULATOR_NAME = "iPhone 15"   # override via --sim

# -------- Optional/soft dependencies --------
try:
    import mss
    MSS_AVAILABLE = True
except Exception:
    MSS_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except Exception:
    CV2_AVAILABLE = False

try:
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

import numpy as np
from PIL import Image

import pyautogui
from pynput import keyboard, mouse

# macOS-only
IS_MAC = sys.platform == "darwin"
if not IS_MAC:
    print("This script is intended for macOS.")
    sys.exit(1)

# -------- Paths/Storage --------
ROOT = Path("recordings")
ROOT.mkdir(exist_ok=True)

# -------- Capture sizes and matching params --------
SNIP_MICRO = (80, 60)
SNIP_SMALL = (160, 120)
SNIP_MEDIUM = (320, 240)
SNIP_LARGE = (480, 360)
SNIP_CONTEXT = (800, 600)

TEMPLATE_CONF_HIGH = 0.85
TEMPLATE_CONF_DEFAULT = 0.78
TEMPLATE_CONF_RELAXED = 0.62
TEMPLATE_CONF_VERY_RELAXED = 0.45

ORB_N_FEATURES = 1500
ORB_SCALE_FACTOR = 1.2
ORB_N_LEVELS = 8
ORB_MATCH_RATIO = 0.7
ORB_MIN_GOOD_MATCHES = 6

UI_CHANGE_DETECTION_DELAY = 0.3

FOCUS_SETTLE = 0.15

SCROLL_MULTIPLIER = 120
POST_SCROLL_WAIT = 0.12

# Emergency abort hotkey: Ctrl+Shift+A+F+K
ABORT_KEYS = {"<ctrl>", "<shift>", "a", "f", "k"}
abort_event = Event()

# -------- Data structures --------
class ActionType(Enum):
    MOUSE_CLICK = "mouse_click"
    MOUSE_MOVE = "mouse_move"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"

@dataclass
class UIState:
    timestamp: float
    screenshot_hash: Optional[str] = None
    visible_text: Optional[List[str]] = None

@dataclass
class EnhancedEvent:
    timestamp: float
    action_type: ActionType
    x: int
    y: int
    data: Dict[str, Any]

    img_micro: Optional[str] = None
    img_small: Optional[str] = None
    img_medium: Optional[str] = None
    img_large: Optional[str] = None
    img_context: Optional[str] = None
    img_origins: Optional[Dict[str, Dict[str, int]]] = None

    ui_state_before: Optional[UIState] = None
    ui_state_after: Optional[UIState] = None
    action_validated: bool = False
    validation_confidence: float = 0.0

    ocr_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["action_type"] = self.action_type.value
        return d

# -------- Utilities --------
def run(cmd: List[str], capture_output=True, text=True, check=False, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture_output, text=text, check=check, env=env)

def mac_notify(msg: str):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "simmy"'],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        pass

def _safe_screenshot() -> Optional[Image.Image]:
    if MSS_AVAILABLE:
        try:
            with mss.mss() as s:
                monitor = s.monitors[0]
                s_img = s.grab(monitor)
                img = Image.frombytes("RGB", s_img.size, s_img.rgb)
                return img
        except Exception:
            pass
    try:
        img = pyautogui.screenshot()
        return img
    except Exception:
        return None

def _img_hash(img: Image.Image) -> Optional[str]:
    try:
        md = hashlib.sha256(img.tobytes()).hexdigest()[:16]
        return md
    except Exception:
        return None

def _capture_ui_state() -> UIState:
    img = _safe_screenshot()
    st = UIState(timestamp=time.time())
    if img:
        st.screenshot_hash = _img_hash(img)
        if OCR_AVAILABLE:
            try:
                text = pytesseract.image_to_string(img)
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                st.visible_text = lines
            except Exception:
                pass
    return st

def _detect_ui_changes(before_hash: Optional[str], after_hash: Optional[str]) -> bool:
    return bool(before_hash and after_hash and before_hash != after_hash)

def _extract_text_from_region(img: Image.Image, x: int, y: int, w: int, h: int) -> Optional[str]:
    if not OCR_AVAILABLE:
        return None
    try:
        region = img.crop((x, y, x + w, y + h))
        region = region.convert("L")
        region = region.resize((region.width * 2, region.height * 2), Image.LANCZOS)
        text = pytesseract.image_to_string(region, config="--psm 8").strip()
        return text or None
    except Exception:
        return None

def _save_multi_snips(outdir: Path, x: int, y: int) -> Tuple[Dict[str, str], Dict[str, Dict[str, int]]]:
    img = _safe_screenshot()
    if img is None:
        return {}, {}
    sw, sh = img.size
    sizes = {
        "micro": SNIP_MICRO,
        "small": SNIP_SMALL,
        "medium": SNIP_MEDIUM,
        "large": SNIP_LARGE,
        "context": SNIP_CONTEXT,
    }
    results: Dict[str, str] = {}
    origins: Dict[str, Dict[str, int]] = {}
    for name, (w, h) in sizes.items():
        left = max(0, x - w // 2)
        top = max(0, y - h // 2)
        right = min(sw, left + w)
        bottom = min(sh, top + h)
        if right <= left or bottom <= top:
            continue
        crop = img.crop((left, top, right, bottom))
        ih = _img_hash(crop) or f"nohash_{int(time.time()*1000)}"
        path = outdir / "images" / f"{name}_{ih}.png"
        try:
            crop.save(path)
            results[f"img_{name}"] = str(path)
            origins[f"img_{name}"] = {"left": left, "top": top}
        except Exception:
            continue
    return results, origins

# -------- AppleScript helpers --------
def osa(script: str) -> str:
    p = run(["osascript", "-e", script])
    return (p.stdout or "").strip()

def activate_simulator():
    osa('tell application "Simulator" to activate')

def set_simulator_window_bounds(x: int, y: int, w: int, h: int):
    # Set bounds of front window of Simulator
    osa(
        'tell application "System Events" to tell process "Simulator" '
        f'to set position of front window to {{{x}, {y}}}'
    )
    osa(
        'tell application "System Events" to tell process "Simulator" '
        f'to set size of front window to {{{w}, {h}}}'
    )

def get_simulator_front_window_bounds() -> Optional[Tuple[int, int, int, int]]:
    try:
        x = int(osa('tell application "System Events" to tell process "Simulator" to get item 1 of position of front window'))
        y = int(osa('tell application "System Events" to tell process "Simulator" to get item 2 of position of front window'))
        w = int(osa('tell application "System Events" to tell process "Simulator" to get item 1 of size of front window'))
        h = int(osa('tell application "System Events" to tell process "Simulator" to get item 2 of size of front window'))
        return (x, y, w, h)
    except Exception:
        return None

# -------- simctl helpers --------
def simctl_json(args: List[str]) -> Dict[str, Any]:
    p = run(["xcrun", "simctl"] + args + ["--json"])
    try:
        return json.loads(p.stdout)
    except Exception:
        return {}

def simctl(args: List[str]) -> subprocess.CompletedProcess:
    return run(["xcrun", "simctl"] + args)

def find_runtime_identifier() -> Optional[str]:
    # Pick newest available iOS runtime
    data = simctl_json(["list", "runtimes"])
    runtimes = data.get("runtimes", [])
    ios = [
        r for r in runtimes
        if r.get("identifier", "").startswith("com.apple.CoreSimulator.SimulationRuntime.iOS")
        and r.get("isAvailable", True)
    ]
    if not ios:
        return None
    # Sort by version
    def ver_key(r):
        m = re.search(r"iOS (\d+)\.(\d+)", r.get("name", ""))
        if not m:
            return (0, 0)
        return (int(m.group(1)), int(m.group(2)))
    ios.sort(key=ver_key)
    return ios[-1].get("identifier")

def find_or_create_device(sim_name: str) -> Optional[str]:
    # Find by exact name
    devices = simctl_json(["list", "devices"]).get("devices", {})
    for rt, devs in devices.items():
        for d in devs:
            if d.get("name") == sim_name:
                return d.get("udid")
    # Create one
    rt_id = find_runtime_identifier()
    if not rt_id:
        print("No iOS runtime found.")
        return None
    p = simctl(["create", sim_name, "com.apple.CoreSimulator.SimDeviceType.iPhone-14", rt_id])
    if p.returncode != 0 or not p.stdout.strip():
        print("Failed to create simulator device.")
        return None
    return p.stdout.strip()

def boot_device_new_instance(udid: str) -> bool:
    # Boot device and open a new Simulator instance bound to UDID
    simctl(["boot", udid])
    time.sleep(1.0)
    # New app instance
    p = run(["open", "-na", "Simulator", "--args", "-CurrentDeviceUDID", udid])
    if p.returncode != 0:
        print("Failed to open new Simulator instance.")
        return False
    # Give time to start
    time.sleep(3.0)
    return True

def is_app_installed(udid: str, bundle_id: str) -> bool:
    # Use get_app_container to check installation
    p = simctl(["get_app_container", udid, bundle_id])
    return p.returncode == 0

def install_app(udid: str, app_path: str) -> bool:
    if not app_path or not Path(app_path).exists():
        print("App path missing or invalid; cannot install.")
        return False
    p = simctl(["install", udid, app_path])
    return p.returncode == 0

def launch_app(udid: str, bundle_id: str) -> bool:
    p = simctl(["launch", udid, bundle_id])
    return p.returncode == 0

# -------- Record/Replay core --------
class Recorder:
    def __init__(self, outdir: Path, sim_name: str):
        self.outdir = outdir
        (self.outdir / "images").mkdir(parents=True, exist_ok=True)
        self.events: List[EnhancedEvent] = []
        self.start_time = 0.0
        self.running = False
        self._mouse_listener = None
        self._kbd_listener = None
        self.sim_name = sim_name
        self.window_rect = None  # (x, y, w, h)

    def _ts(self) -> float:
        return round(time.time() - self.start_time, 4)

    def _validate_effect(self, ev: EnhancedEvent):
        time.sleep(UI_CHANGE_DETECTION_DELAY)
        after = _capture_ui_state()
        ev.ui_state_after = after
        if ev.ui_state_before:
            changed = _detect_ui_changes(ev.ui_state_before.screenshot_hash, after.screenshot_hash)
            ev.action_validated = changed
            ev.validation_confidence = 1.0 if changed else 0.0

    def _record_event(self, action_type: ActionType, x: int, y: int, data: Dict[str, Any]):
        ui_before = _capture_ui_state()
        ev = EnhancedEvent(
            timestamp=self._ts(),
            action_type=action_type,
            x=x,
            y=y,
            data=data,
            ui_state_before=ui_before,
        )
        # Save snips and OCR
        snips, origins = _save_multi_snips(self.outdir, x, y)
        for k, v in snips.items():
            setattr(ev, k, v)
        ev.img_origins = origins

        img = _safe_screenshot()
        if img is not None:
            ev.ocr_text = _extract_text_from_region(img, max(0, x - 50), max(0, y - 25), 100, 50)

        self.events.append(ev)
        # Validate effect for clicks/scrolls
        if action_type in (ActionType.MOUSE_CLICK, ActionType.MOUSE_SCROLL):
            self._validate_effect(ev)

    def on_move(self, x: int, y: int):
        if not self.running or abort_event.is_set():
            return
        # record raw moves only if needed (disabled by default to reduce noise)
        # self._record_event(ActionType.MOUSE_MOVE, x, y, {})
        return

    def on_click(self, x: int, y: int, button, pressed: bool):
        if not self.running or abort_event.is_set():
            return
        if pressed:
            self._record_event(ActionType.MOUSE_CLICK, x, y, {"button": str(button), "pressed": True})

    def on_scroll(self, x: int, y: int, dx: int, dy: int):
        if not self.running or abort_event.is_set():
            return
        self._record_event(ActionType.MOUSE_SCROLL, x, y, {"dx": dx, "dy": dy})

    def on_press(self, key):
        if not self.running or abort_event.is_set():
            return
        try:
            k = key.char
        except Exception:
            k = str(key)
        self.events.append(
            EnhancedEvent(
                timestamp=self._ts(),
                action_type=ActionType.KEY_PRESS,
                x=0,
                y=0,
                data={"key": k},
            )
        )

    def on_release(self, key):
        if not self.running or abort_event.is_set():
            return
        try:
            k = key.char
        except Exception:
            k = str(key)
        self.events.append(
            EnhancedEvent(
                timestamp=self._ts(),
                action_type=ActionType.KEY_RELEASE,
                x=0,
                y=0,
                data={"key": k},
            )
        )

    def start(self):
        print("Recording... Press Enter in terminal to stop. Emergency abort: Ctrl+Shift+A+F+K")
        self.start_time = time.time()
        self.running = True
        self._mouse_listener = mouse.Listener(
            on_move=self.on_move, on_click=self.on_click, on_scroll=self.on_scroll
        )
        self._kbd_listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self._mouse_listener.start()
        self._kbd_listener.start()

    def stop(self):
        self.running = False
        try:
            if self._mouse_listener:
                self._mouse_listener.stop()
            if self._kbd_listener:
                self._kbd_listener.stop()
        except Exception:
            pass

    def save(self, name: str):
        payload = {
            "meta": {
                "simulator_name": self.sim_name,
                "recorded_at": int(time.time()),
                "platform": "macOS",
                "version": "simmy_v1",
            },
            "events": [e.to_dict() for e in self.events],
        }
        (self.outdir / "script.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Saved recording to {self.outdir}")
        print(f"Events captured: {len(self.events)}")

# ---- Matching and replay ----
def _cv2_template_match(template_path: Path, screenshot: Image.Image, confidence: float) -> Optional[Tuple[int, int, float]]:
    if not CV2_AVAILABLE:
        return None
    try:
        template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template is None:
            return None
        screenshot_np = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        res = cv2.matchTemplate(screenshot_np, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= confidence:
            h, w = template.shape[:2]
            cx = max_loc[0] + w // 2
            cy = max_loc[1] + h // 2
            return (cx, cy, float(max_val))
        return None
    except Exception:
        return None

def _orb_match(template_path: Path, screenshot: Image.Image) -> Optional[Tuple[int, int, float]]:
    if not CV2_AVAILABLE:
        return None
    try:
        orb = cv2.ORB_create(
            nfeatures=ORB_N_FEATURES, scaleFactor=ORB_SCALE_FACTOR, nlevels=ORB_N_LEVELS
        )
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        template = Image.open(template_path).convert("RGB")
        template_np = np.array(template)[:, :, ::-1]
        screen_np = np.array(screenshot.convert("RGB"))[:, :, ::-1]
        g1 = cv2.cvtColor(template_np, cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(screen_np, cv2.COLOR_BGR2GRAY)
        kp1, des1 = orb.detectAndCompute(g1, None)
        kp2, des2 = orb.detectAndCompute(g2, None)
        if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
            return None
        matches = bf.knnMatch(des1, des2, k=2)
        good = []
        for pair in matches:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < ORB_MATCH_RATIO * n.distance:
                good.append(m)
        if len(good) < ORB_MIN_GOOD_MATCHES:
            return None
        pts = [kp2[m.trainIdx].pt for m in good]
        cx = int(sum(p[0] for p in pts) / len(pts))
        cy = int(sum(p[1] for p in pts) / len(pts))
        conf = min(1.0, len(good) / max(1, len(kp1)))
        return (cx, cy, float(conf))
    except Exception:
        return None

def _ocr_match(target_text: str, screenshot: Image.Image) -> Optional[Tuple[int, int, float]]:
    if not OCR_AVAILABLE or not target_text:
        return None
    try:
        data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)
        for i, txt in enumerate(data.get("text", [])):
            if not txt:
                continue
            if target_text in txt:
                x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                return (x + w // 2, y + h // 2, 0.7 if txt == target_text else 0.6)
        return None
    except Exception:
        return None

def _try_find_click_target(event: Dict[str, Any], script_dir: Path) -> Optional[Tuple[int, int]]:
    screenshot = _safe_screenshot()
    if screenshot is None:
        return None
    # Try images
    img_attrs = ["img_small", "img_medium", "img_large", "img_micro", "img_context"]
    confs = [TEMPLATE_CONF_HIGH, TEMPLATE_CONF_DEFAULT, TEMPLATE_CONF_RELAXED, TEMPLATE_CONF_VERY_RELAXED]
    for attr in img_attrs:
        pth = event.get(attr)
        if not pth:
            continue
        candidate = Path(pth)
        if not candidate.exists():
            # maybe saved absolute in recording; try name under images
            candidate2 = script_dir / "images" / candidate.name
            if candidate2.exists():
                candidate = candidate2
            else:
                continue
        # Template
        for c in confs:
            hit = _cv2_template_match(candidate, screenshot, c)
            if hit:
                return (hit[0], hit[1])
        # ORB
        hit = _orb_match(candidate, screenshot)
        if hit:
            return (hit[0], hit[1])
    # OCR
    tt = event.get("ocr_text") or ""
    if tt:
        hit = _ocr_match(tt, screenshot)
        if hit:
            return (hit[0], hit[1])
    return None

def _translate_coord(recorded_xy: Tuple[int, int], rec_win: Tuple[int, int, int, int], cur_win: Tuple[int, int, int, int]) -> Tuple[int, int]:
    rx, ry = recorded_xy
    r_x, r_y, r_w, r_h = rec_win
    c_x, c_y, c_w, c_h = cur_win
    local_x = rx - r_x
    local_y = ry - r_y
    sx = c_w / max(1, r_w)
    sy = c_h / max(1, r_h)
    nx = int(round(c_x + local_x * sx))
    ny = int(round(c_y + local_y * sy))
    return (nx, ny)

def _intelligent_click(x: int, y: int, max_radius: int = 24) -> bool:
    try:
        pyautogui.moveTo(x, y, duration=0.02)
        pyautogui.click(x=x, y=y)
        time.sleep(0.05)
        return True
    except Exception:
        pass
    # try small spiral retries
    steps = [(dx, dy) for dx in (-8, 0, 8) for dy in (-8, 0, 8)]
    for dx, dy in steps:
        try:
            px, py = x + dx, y + dy
            pyautogui.moveTo(px, py, duration=0.02)
            pyautogui.click(x=px, y=py)
            time.sleep(0.05)
            return True
        except Exception:
            continue
    # expand radius
    for r in (12, 18, max_radius):
        for dx in (-r, 0, r):
            for dy in (-r, 0, r):
                try:
                    px, py = x + dx, y + dy
                    pyautogui.moveTo(px, py, duration=0.02)
                    pyautogui.click(x=px, y=py)
                    time.sleep(0.05)
                    return True
                except Exception:
                    continue
    return False

def _perform_scroll_at(x: int, y: int, dy: int):
    pyautogui.moveTo(x, y, duration=0.02)
    pyautogui.scroll(int(dy * SCROLL_MULTIPLIER), x=x, y=y)
    time.sleep(POST_SCROLL_WAIT)

def _replay_key_event(event: Dict[str, Any]) -> bool:
    k = event.get("data", {}).get("key", "")
    try:
        if len(k) == 1 and k.isprintable():
            if event.get("action_type") in ("key_press", ActionType.KEY_PRESS.value):
                pyautogui.keyDown(k)
            else:
                pyautogui.keyUp(k)
        else:
            mapped = {
                "Key.enter": "enter",
                "Key.tab": "tab",
                "Key.space": "space",
                "Key.backspace": "backspace",
                "Key.shift": "shift",
                "Key.ctrl": "ctrl",
                "Key.alt": "alt",
                "Key.esc": "esc",
                "Key.up": "up",
                "Key.down": "down",
                "Key.left": "left",
                "Key.right": "right",
            }.get(k, k.replace("Key.", ""))
            if event.get("action_type") in ("key_press", ActionType.KEY_PRESS.value):
                pyautogui.keyDown(mapped)
            else:
                pyautogui.keyUp(mapped)
        return True
    except Exception:
        return False

def replay_script(script_dir: Path, sim_name: str, bundle_id: str, app_path: Optional[str], debug: bool) -> bool:
    script_file = script_dir / "script.json"
    if not script_file.exists():
        print(f"Missing script.json in {script_dir}")
        return False
    data = json.loads(script_file.read_text(encoding="utf-8"))
    events: List[Dict[str, Any]] = data.get("events", [])
    if not events:
        print("No events to replay.")
        return False

    # simctl: ensure device, boot, new instance, install if needed, launch
    udid = find_or_create_device(sim_name)
    if not udid:
        print("Failed to resolve simulator device.")
        return False
    if not boot_device_new_instance(udid):
        print("Failed to boot/open Simulator instance.")
        return False

    # Activate and size window to a predictable region
    activate_simulator()
    time.sleep(FOCUS_SETTLE)
    # Place window top-left with fixed size to reduce scaling drift
    set_simulator_window_bounds(50, 50, 420, 860)
    time.sleep(0.3)
    win_rect = get_simulator_front_window_bounds() or (50, 50, 420, 860)

    # Install if missing
    if not is_app_installed(udid, bundle_id):
        if not app_path:
            print("App not installed and no --app-path provided. Cannot proceed.")
            return False
        print("Installing app...")
        if not install_app(udid, app_path):
            print("Failed to install app.")
            return False

    print("Launching app...")
    if not launch_app(udid, bundle_id):
        print("Failed to launch app.")
        return False

    time.sleep(2.0)  # let app render

    # Replay events
    print(f"Replaying {len(events)} events...")
    start_t = time.time()
    last_ts = 0.0
    # For coordinate translation, capture the recorded window rect if present; else use screen
    rec_win = None
    meta = data.get("meta", {})
    # As we did not store specific rec window rect in this mac version, derive from first event image origin if available
    # fallback to full screen
    screen_w, screen_h = pyautogui.size()
    rec_win = (0, 0, screen_w, screen_h)

    success = 0
    fail = 0

    for i, ev in enumerate(events):
        if abort_event.is_set():
            print("Abort signal received.")
            break
        try:
            t = float(ev.get("timestamp", 0.0))
            wait = max(0.0, t - last_ts)
            if wait > 0:
                time.sleep(wait)
            last_ts = t

            etype = ev.get("action_type")
            if etype in ("key_press", "key_release"):
                ok = _replay_key_event(ev)
                success += 1 if ok else 0
                fail += 0 if ok else 1
                continue

            if etype == "mouse_scroll":
                target = _try_find_click_target(ev, script_dir)
                if not target:
                    # fallback coordinate translation into current window
                    nx, ny = _translate_coord((ev["x"], ev["y"]), rec_win, win_rect)
                else:
                    nx, ny = target
                _perform_scroll_at(nx, ny, ev.get("data", {}).get("dy", 0))
                success += 1
                continue

            if etype == "mouse_click":
                target = _try_find_click_target(ev, script_dir)
                if not target:
                    nx, ny = _translate_coord((ev["x"], ev["y"]), rec_win, win_rect)
                else:
                    nx, ny = target
                ok = _intelligent_click(nx, ny)
                if debug:
                    print(f"[{i}] click at ({nx},{ny}) {'ok' if ok else 'fail'}")
                if ok:
                    success += 1
                else:
                    fail += 1
                continue

            # ignore moves by default
        except Exception as e:
            if debug:
                print(f"[{i}] Exception: {e}")
            fail += 1

    print(f"Replay done: {success} success, {fail} failed")
    return fail == 0

# -------- Abort hotkey listener --------
class AbortHotkey:
    def __init__(self):
        self.pressed = set()

    def on_press(self, key):
        try:
            if key == keyboard.Key.ctrl or key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                self.pressed.add("<ctrl>")
            elif key == keyboard.Key.shift or key == keyboard.Key.shift_l or key == keyboard.Key.shift_r:
                self.pressed.add("<shift>")
            else:
                ch = key.char.lower() if hasattr(key, "char") and key.char else str(key)
                if ch in ("a", "f", "k"):
                    self.pressed.add(ch)
        except Exception:
            pass
        if ABORT_KEYS.issubset(self.pressed):
            abort_event.set()
            mac_notify("Abort hotkey triggered")
            return False  # stop listener

    def on_release(self, key):
        # keep state; we only need to detect once
        pass

def start_abort_listener() -> keyboard.Listener:
    ah = AbortHotkey()
    listener = keyboard.Listener(on_press=ah.on_press, on_release=ah.on_release)
    listener.start()
    return listener

# -------- CLI --------
def main():
    p = argparse.ArgumentParser(description="simmy - macOS iOS Simulator UI recorder/replayer")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("record", help="Record a new session")
    pr.add_argument("--name", required=True, help="Recording name (directory under recordings/)")
    pr.add_argument("--sim", default=DEFAULT_SIMULATOR_NAME, help="Simulator device name (e.g., 'iPhone 15')")

    pp = sub.add_parser("replay", help="Replay a recorded session")
    pp.add_argument("--dir", required=True, help="Recording dir (path or name under recordings/)")
    pp.add_argument("--sim", default=DEFAULT_SIMULATOR_NAME, help="Simulator device name")
    pp.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="App bundle id")
    pp.add_argument("--app-path", default=None, help="Path to .app (required if app not installed)")
    pp.add_argument("--debug", action="store_true")

    # Short aliases to match your request too
    p.add_argument("--record", action="store_true", help="Shortcut to: record --name <auto> --sim <default>")
    p.add_argument("--replay", type=str, help="Shortcut to: replay --dir <path>")

    args = p.parse_args()

    # Map shortcuts to subcommands
    if args.record and not args.cmd:
        rec_name = f"rec_{int(time.time())}"
        args.cmd = "record"
        args.name = rec_name
        args.sim = DEFAULT_SIMULATOR_NAME
    elif args.replay and not args.cmd:
        args.cmd = "replay"
        args.dir = args.replay
        args.sim = DEFAULT_SIMULATOR_NAME
        args.bundle_id = DEFAULT_BUNDLE_ID
        args.app_path = None
        args.debug = False

    # Start abort listener
    listener = start_abort_listener()

    if args.cmd == "record":
        outdir = ROOT / args.name
        outdir.mkdir(parents=True, exist_ok=True)
        rec = Recorder(outdir, args.sim)
        rec.start()
        input()  # wait for Enter to stop
        rec.stop()
        rec.save(args.name)
        return

    if args.cmd == "replay":
        # Normalize dir
        script_dir = Path(args.dir)
        if script_dir.is_dir() and (script_dir / "script.json").exists():
            pass
        else:
            candidate = ROOT / args.dir
            if candidate.is_dir() and (candidate / "script.json").exists():
                script_dir = candidate
            else:
                print("Recording directory not found or missing script.json")
                return
        ok = replay_script(
            script_dir=script_dir,
            sim_name=args.sim,
            bundle_id=args.bundle_id,
            app_path=args.app_path,
            debug=args.debug,
        )
        if not ok:
            sys.exit(2)
        return

    # Help + examples
    print("Examples:")
    print("  Record: python3 simmy.py record --name my_flow --sim 'iPhone 15'")
    print("    or   python3 simmy.py --record  (auto name, default sim)")
    print("  Replay: python3 simmy.py replay --dir recordings/my_flow --sim 'iPhone 15' --bundle-id com.example.app --app-path /path/MyApp.app --debug")
    print("    or    python3 simmy.py --replay recordings/my_flow")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        abort_event.set()
        print("\nInterrupted.")