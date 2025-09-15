#!/usr/bin/env python3
"""
Simple action capture & replay CLI focused on a target application window.
Stop recording or replay with the global hotkey: Alt+Ctrl+A+F+K
Saves recordings to recordings/<name>/script.json
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any, Dict, List

import pyautogui
import pygetwindow as gw
from pynput import mouse, keyboard

# Optional on Windows to better focus windows
try:
    import win32gui
    import win32con
    WIN32 = True
except Exception:
    WIN32 = False

ROOT = Path("recordings")
ROOT.mkdir(exist_ok=True)

STOP_HOTKEY = "<ctrl>+<alt>+a+f+k"  # pynput GlobalHotKeys style


def choose_window() -> str:
    titles = [t for t in gw.getAllTitles() if t and t.strip()]
    unique = list(dict.fromkeys(titles))
    if not unique:
        print("No windows found.")
        sys.exit(1)
    for i, t in enumerate(unique):
        print(f"{i}: {t}")
    while True:
        sel = input("Choose window index: ").strip()
        if sel.isdigit() and 0 <= int(sel) < len(unique):
            return unique[int(sel)]
        print("Invalid selection.")


def bring_to_front(title_substr: str):
    # Try exact title first, then substring match
    wins = []
    for t in gw.getAllTitles():
        if t and title_substr == t:
            wins = gw.getWindowsWithTitle(t)
            break
    if not wins:
        for t in gw.getAllTitles():
            if t and title_substr in t:
                wins = gw.getWindowsWithTitle(t)
                break
    if not wins:
        return False
    w = wins[0]
    try:
        w.activate()
    except Exception:
        try:
            w.minimize(); time.sleep(0.05); w.restore()
        except Exception:
            pass
    # Windows-specific stronger approach
    if WIN32:
        try:
            hwnd = getattr(w, "_hWnd", None)
            if hwnd:
                win32gui.SetForegroundWindow(hwnd)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
    time.sleep(0.12)  # allow focus to settle
    return True


def is_window_foreground(title_substr: str) -> bool:
    # If exact or containing title matches the active window
    active = None
    try:
        if WIN32:
            hwnd = win32gui.GetForegroundWindow()
            active = win32gui.GetWindowText(hwnd)
    except Exception:
        # fallback to pygetwindow check (best-effort)
        try:
            active = gw.getActiveWindow().title
        except Exception:
            active = None
    if not active:
        return False
    return title_substr in active or active in title_substr


def record(target_title: str, outdir: Path):
    events: List[Dict[str, Any]] = []
    start_t = time.time()
    stop_event = Event()
    print("Recording: press", STOP_HOTKEY.replace("+", " + "), "to stop")

    # global hotkey listener to stop
    def on_stop():
        stop_event.set()

    hk = keyboard.GlobalHotKeys({STOP_HOTKEY: on_stop})
    hk.start()

    # mouse & keyboard handlers
    def mk_ts():
        return round(time.time() - start_t, 4)

    def on_move(x, y):
        if is_window_foreground(target_title):
            events.append({"type": "move", "t": mk_ts(), "x": x, "y": y})

    def on_click(x, y, button, pressed):
        if is_window_foreground(target_title):
            events.append({
                "type": "click",
                "t": mk_ts(),
                "x": x, "y": y,
                "button": str(button), "pressed": pressed
            })

    def on_scroll(x, y, dx, dy):
        if is_window_foreground(target_title):
            events.append({"type": "scroll", "t": mk_ts(), "x": x, "y": y, "dx": dx, "dy": dy})

    def on_key_press(key):
        if is_window_foreground(target_title):
            try:
                k = key.char
            except Exception:
                k = str(key)
            events.append({"type": "key_press", "t": mk_ts(), "key": k})

    def on_key_release(key):
        if is_window_foreground(target_title):
            try:
                k = key.char
            except Exception:
                k = str(key)
            events.append({"type": "key_release", "t": mk_ts(), "key": k})

    ml = mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
    kl = keyboard.Listener(on_press=on_key_press, on_release=on_key_release)
    ml.start(); kl.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.05)
    finally:
        ml.stop(); kl.stop(); hk.stop()

    payload = {
        "meta": {"target_title": target_title, "created": time.time()},
        "events": events,
        "version": "simple_v1"
    }
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "script.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Saved {len(events)} events to {outdir / 'script.json'}")


def replay(script_dir: Path, speed: float = 1.0):
    script_file = script_dir / "script.json"
    if not script_file.exists():
        print("script.json not found in", script_dir)
        return
    with open(script_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    meta = data.get("meta", {})
    events = data.get("events", [])
    target_title = meta.get("target_title", "")
    if not target_title:
        print("No target title recorded. Aborting.")
        return

    print("Replaying against target:", target_title)
    stop_event = Event()

    def on_stop():
        stop_event.set()

    hk = keyboard.GlobalHotKeys({STOP_HOTKEY: on_stop})
    hk.start()

    # Attempt to bring target to front
    if not bring_to_front(target_title):
        print("Warning: target window not found or could not be focused. Replay will attempt to match window when possible.")

    start_t = time.time()
    last_t = 0.0
    for ev in events:
        if stop_event.is_set():
            print("Replay stopped by hotkey.")
            break
        t = ev.get("t", 0) / float(speed)
        wait = t - last_t
        if wait > 0:
            # while waiting, if target not foreground, try to bring it and pause briefly
            waited = 0.0
            while waited < wait:
                if stop_event.is_set():
                    break
                if not is_window_foreground(target_title):
                    # try to re-focus (best effort)
                    bring_to_front(target_title)
                step = min(0.05, wait - waited)
                time.sleep(step)
                waited += step
        last_t = t

        # Only execute action if target is foreground (protect from accidental cross-app replay)
        if not is_window_foreground(target_title):
            # skip this event (safer than clicking other apps)
            if not is_window_foreground(target_title):
                print("Skipping event because target is not foreground:", ev.get("type"))
                continue

        etype = ev.get("type")
        try:
            if etype == "move":
                pyautogui.moveTo(ev["x"], ev["y"])
            elif etype == "click":
                if ev.get("pressed", True):
                    pyautogui.mouseDown(x=ev["x"], y=ev["y"], button=_py_button(ev.get("button")))
                else:
                    pyautogui.mouseUp(x=ev["x"], y=ev["y"], button=_py_button(ev.get("button")))
            elif etype == "scroll":
                pyautogui.scroll(int(ev.get("dy", 0) * 1), x=ev["x"], y=ev["y"])
            elif etype == "key_press":
                _py_key_down(ev.get("key", ""))
            elif etype == "key_release":
                _py_key_up(ev.get("key", ""))
        except Exception as e:
            print("Error during replay action:", e)
    hk.stop()
    print("Replay finished.")


# small helpers to map pynput strings to pyautogui calls
def _py_button(btn_str: str):
    # pynput prints like Button.left
    if not btn_str:
        return "left"
    if "right" in btn_str.lower():
        return "right"
    return "left"


def _py_key_down(k: str):
    if not k:
        return
    if k.startswith("Key."):
        keymap = {
            "Key.enter": "enter",
            "Key.tab": "tab",
            "Key.space": "space",
            "Key.backspace": "backspace",
            "Key.shift": "shift",
            "Key.ctrl": "ctrl",
            "Key.alt": "alt",
            "Key.esc": "esc",
            "Key.up": "up", "Key.down": "down", "Key.left": "left", "Key.right": "right"
        }
        pyautogui.keyDown(keymap.get(k, k.replace("Key.", "")))
    else:
        pyautogui.keyDown(k)


def _py_key_up(k: str):
    if not k:
        return
    if k.startswith("Key."):
        keymap = {
            "Key.enter": "enter",
            "Key.tab": "tab",
            "Key.space": "space",
            "Key.backspace": "backspace",
            "Key.shift": "shift",
            "Key.ctrl": "ctrl",
            "Key.alt": "alt",
            "Key.esc": "esc",
            "Key.up": "up", "Key.down": "down", "Key.left": "left", "Key.right": "right"
        }
        pyautogui.keyUp(keymap.get(k, k.replace("Key.", "")))
    else:
        pyautogui.keyUp(k)


def main():
    print("Action capture & replay CLI")
    print("1) Record")
    print("2) Replay")
    choice = input("Choose (1/2): ").strip()
    if choice == "1":
        name = input("Recording name: ").strip()
        if not name:
            print("Name required.")
            return
        target = choose_window()
        print("Bringing target to front...")
        bring_to_front(target)
        outdir = ROOT / name
        record(target, outdir)
    elif choice == "2":
        # list recordings
        recs = [d for d in sorted(ROOT.iterdir()) if d.is_dir() and (d / "script.json").exists()]
        if not recs:
            print("No recordings found.")
            return
        for i, d in enumerate(recs):
            print(f"{i}: {d.name}")
        sel = input("Choose recording index: ").strip()
        if not sel.isdigit() or not (0 <= int(sel) < len(recs)):
            print("Invalid selection.")
            return
        replay(recs[int(sel)])
    else:
        print("Nothing to do.")


if __name__ == "__main__":
    main()