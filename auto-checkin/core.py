# -*- coding: utf-8 -*-
"""trae-auto-checkin 底层核心。

职责：目录解析、日志、状态读写、flows/config 载入、Win32 窗口与输入、截图、多尺度模板匹配。
"""

import ctypes
import datetime
import glob
import json
import os
import re
import shutil
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np
import psutil
from PIL import ImageGrab

IS_FROZEN = bool(getattr(sys, "frozen", False))


def _exe_dir():
    """打包后是 exe 所在目录，源码运行时是脚本所在目录。"""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _exe_dir()
BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)  # PyInstaller 内置资源目录


def _is_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _data_dir():
    """绿色版优先：配置与模板放 exe 同目录；目录只读时退回 %APPDATA%。"""
    if _is_writable(APP_DIR):
        return APP_DIR
    fallback = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "trae-auto-checkin")
    os.makedirs(fallback, exist_ok=True)
    return fallback


DATA_DIR = _data_dir()

BUNDLE_TPL_DIR = os.path.join(BUNDLE_DIR, "templates")
BUNDLE_FLOWS = os.path.join(BUNDLE_DIR, "flows.json")

TEMPLATE_DIR = os.path.join(DATA_DIR, "templates")
FLOWS_FILE = os.path.join(DATA_DIR, "flows.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "checkin.log")
SHOT_DIR = os.path.join(DATA_DIR, "shots")

TASK_NAME = "trae-auto-checkin"
LEGACY_TASKS = ("TraeWork_WorkBuddy_DailyCheckin",)

DEFAULT_CONFIG = {
    "version": 2,
    "lock_guard": True,
    "schedule": {
        "time": "08:00",
        "retry_interval_min": 30,
        "retry_duration_hours": 16,
        "on_logon": True,
    },
    "apps": {},  # 由 flows.json 里的应用名自动补齐为 {名称: {"enabled": bool, "exe": str}}
}


# --------------------------------------------------------------------------- 日志
def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- 运行期文件
def _copy_missing(src, dst):
    if os.path.exists(src) and not os.path.exists(dst):
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            pass
    return False


def ensure_runtime_files():
    """首次运行把内置的 flows.json 和模板复制到数据目录，之后用户可自由修改/校准覆盖。"""
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    _copy_missing(BUNDLE_FLOWS, FLOWS_FILE)
    if os.path.abspath(BUNDLE_TPL_DIR) != os.path.abspath(TEMPLATE_DIR):
        for path in glob.glob(os.path.join(BUNDLE_TPL_DIR, "*.png")):
            _copy_missing(path, os.path.join(TEMPLATE_DIR, os.path.basename(path)))


# --------------------------------------------------------------------------- 配置
def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_flows():
    ensure_runtime_files()
    flows = _read_json(FLOWS_FILE, None)
    if not flows:
        flows = _read_json(BUNDLE_FLOWS, None)
    return flows or {"apps": []}


def load_config():
    cfg = _deep_merge(DEFAULT_CONFIG, _read_json(CONFIG_FILE, {}))
    for app in load_flows().get("apps", []):
        slot = cfg["apps"].setdefault(app["name"], {})
        slot.setdefault("enabled", True)
        slot.setdefault("exe", "")
    return cfg


def save_config(cfg):
    _write_json(CONFIG_FILE, cfg)


def app_settings(cfg, name):
    return cfg.get("apps", {}).get(name, {})


# --------------------------------------------------------------------------- 状态
def today():
    return datetime.date.today().isoformat()


def load_state():
    st = _read_json(STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    if st.get("date") != today():
        st = {"date": today(), "done": [], "detail": {}, "issues": {}}
    st.setdefault("done", [])
    st.setdefault("detail", {})
    st.setdefault("issues", {})
    return st


def save_state(st):
    _write_json(STATE_FILE, st)


# --------------------------------------------------------------------------- 模板
def template_file(name):
    path = os.path.join(TEMPLATE_DIR, name)
    return path if os.path.exists(path) else None


def missing_templates():
    """返回 flows.json 里被引用但本地不存在的模板文件名。"""
    names = []
    for app in load_flows().get("apps", []):
        for step in app.get("steps", []):
            for key in ("template",):
                if step.get(key):
                    names.append(step[key])
            for key in ("markers",):
                names.extend(step.get(key) or [])
    return sorted({n for n in names if not template_file(n)})


# --------------------------------------------------------------------------- Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetProcessDPIAware()  # 统一使用物理像素坐标，避免缩放导致点击偏移

user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
user32.keybd_event.argtypes = [ctypes.c_byte, ctypes.c_byte, wintypes.DWORD, ctypes.c_void_p]

SW_SHOW = 5
SW_RESTORE = 9
VK_MENU, VK_ESC = 0x12, 0x1B
LEFTDOWN, LEFTUP = 0x0002, 0x0004
KEYUP = 0x0002


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def is_locked():
    """锁屏时 LogonUI 进程存在，此时模拟点击无效，交给计划任务稍后重试。"""
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == "logonui.exe":
            return True
    return False


def find_window(proc_name, title_re):
    pids = set()
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == proc_name.lower():
            pids.add(p.pid)
    if not pids:
        return None
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if re.search(title_re, buf.value):
                    found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_target_on_top(hwnd, x, y):
    """点击点最上层窗口是否属于目标进程，防止浏览器等程序抢前台后误点。"""
    top = user32.WindowFromPoint(_POINT(int(x), int(y)))
    return bool(top) and pid_of(top) == pid_of(hwnd)


def activate(hwnd, tries=3):
    """把窗口切到前台。Windows 有前台锁定，先按一下 Alt 再 SetForegroundWindow 可绕过。
    最小化的窗口必须先 SW_RESTORE，否则 SW_SHOW 不会还原。"""
    for _ in range(tries):
        user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
        user32.keybd_event(VK_MENU, 0, 0, None)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK_MENU, 0, KEYUP, None)
        time.sleep(0.5)
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.4)
    return False


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(LEFTDOWN, 0, 0, 0, None)
    user32.mouse_event(LEFTUP, 0, 0, 0, None)


def press_esc():
    user32.keybd_event(VK_ESC, 0, 0, None)
    user32.keybd_event(VK_ESC, 0, KEYUP, None)


def click_in_app(hwnd, x, y):
    if not is_target_on_top(hwnd, x, y):
        log("点击位置被其他窗口遮挡，重新激活目标窗口")
        activate(hwnd)
        time.sleep(0.4)
    click(x, y)


def ensure_active(hwnd):
    """截图匹配前确认目标窗口没有被别的程序盖住，否则重新切前台。"""
    r = window_rect(hwnd)
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    if not is_target_on_top(hwnd, cx, cy):
        log("目标窗口被遮挡，重新切到前台")
        activate(hwnd)
        time.sleep(0.5)


# --------------------------------------------------------------------------- 截图与匹配
def grab_screen():
    """截取主屏（PIL 图像）。"""
    return ImageGrab.grab()


def grab_bgr():
    return cv2.cvtColor(np.array(grab_screen()), cv2.COLOR_RGB2BGR)


def save_shot(app_name, tag=""):
    os.makedirs(SHOT_DIR, exist_ok=True)
    suffix = ("_" + tag) if tag else ""
    path = os.path.join(SHOT_DIR, "%s_%s%s.png" % (today(), app_name, suffix))
    try:
        grab_screen().save(path)
    except Exception:
        return ""
    return path


# 多尺度匹配：不同电脑的分辨率/DPI 缩放会让同一按钮呈现不同像素尺寸，逐个尺度试一遍
TEMPLATE_SCALES = (1.0, 0.9, 1.1, 0.8, 1.2, 0.7, 1.35, 0.6, 1.5, 0.5, 1.75, 2.0)
EARLY_EXIT_SCORE = 0.94  # 1.0 倍尺度已高度确信时直接结束，省掉后续尺度


def find_template(name, threshold=0.8):
    """在全屏范围内查找模板，返回 (中心x, 中心y, 相似度, 命中尺度)，未命中返回 None。"""
    path = template_file(name)
    if not path:
        log("模板缺失：%s（可在设置界面点「校准」现场裁剪）" % name)
        return None
    tpl0 = cv2.imread(path)
    if tpl0 is None:
        log("模板无法读取：%s" % path)
        return None

    screen = grab_bgr()
    sh, sw = screen.shape[:2]
    best = None
    for scale in TEMPLATE_SCALES:
        tw, th = int(round(tpl0.shape[1] * scale)), int(round(tpl0.shape[0] * scale))
        if tw < 6 or th < 6 or tw >= sw or th >= sh:
            continue
        if scale == 1.0:
            tpl = tpl0
        else:
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            tpl = cv2.resize(tpl0, (tw, th), interpolation=interp)
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        score = float(score)
        if best is None or score > best[2]:
            best = (loc[0] + tw // 2, loc[1] + th // 2, score, scale)
        if scale == 1.0 and score >= EARLY_EXIT_SCORE:
            break

    if best and best[2] >= threshold:
        return best
    return None
