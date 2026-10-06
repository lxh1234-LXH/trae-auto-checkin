# -*- coding: utf-8 -*-
"""应用定位：在任意电脑上找到目标 App 的 exe 路径。

依次尝试：手动指定 → 运行中的进程 → 注册表卸载信息 → 常见安装目录 → 开始菜单快捷方式。
"""

import ctypes
import os
import subprocess

import psutil

CREATE_NO_WINDOW = 0x08000000
_DRIVE_FIXED = 3
_MAX_VISITED = 4000
_SKIP_TOKENS = ("unins", "uninstall", "setup", "install", "crashpad", "crashreport",
                "update", "updater", "helper", "service", "report")


def find_exe(proc, hints, manual=""):
    """返回 (exe 路径, 来源说明)，都找不到时返回 ("", "")。"""
    if manual and os.path.isfile(manual):
        return manual, "手动指定"

    strategies = (
        (_from_process, "运行中的进程"),
        (_from_registry, "注册表卸载信息"),
        (_from_common_dirs, "常见安装目录"),
        (_from_start_menu, "开始菜单快捷方式"),
    )
    for func, source in strategies:
        try:
            hit = func(proc, hints)
        except Exception:
            hit = None
        if hit and os.path.isfile(hit):
            return hit, source
    return "", ""


# --------------------------------------------------------------------------- 进程
def _from_process(proc, hints):
    target = proc.lower()
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info.get("name") or "").lower() != target:
                continue
            exe = p.info.get("exe")
        except Exception:
            continue
        if exe and os.path.isfile(exe):
            return exe
    return None


# --------------------------------------------------------------------------- 注册表
_UNINSTALL_KEYS = (
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
)


def _registry_entries():
    import winreg
    roots = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    for sub, root_name in _UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(roots[root_name], sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                            entry = {}
                            for field in ("DisplayName", "InstallLocation", "DisplayIcon"):
                                try:
                                    entry[field] = winreg.QueryValueEx(sk, field)[0]
                                except OSError:
                                    entry[field] = ""
                            yield entry
                    except OSError:
                        continue
        except OSError:
            continue


def _from_registry(proc, hints):
    lows = [h.lower() for h in hints]
    for entry in _registry_entries():
        display = (entry.get("DisplayName") or "").lower()
        if display and not any(h in display for h in lows):
            continue
        icon = (entry.get("DisplayIcon") or "").split(",")[0].strip('" ')
        if icon.lower().endswith(".exe") and os.path.isfile(icon):
            return icon
        location = entry.get("InstallLocation") or ""
        if location and os.path.isdir(location):
            hit = _exe_in(location, lows, depth=2)
            if hit:
                return hit
    return None


# --------------------------------------------------------------------------- 文件系统
def _fixed_drives():
    """只取本地固定磁盘，避免在光驱/网络盘上卡住。"""
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    drives = []
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if not (mask & (1 << i)):
            continue
        root = "%s:\\" % letter
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(root) == _DRIVE_FIXED:
                drives.append(root)
        except Exception:
            continue
    return drives


def _search_roots():
    roots = []
    for var in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(var)
        if value:
            roots.append(value)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Programs"))
    roots.extend(_fixed_drives())
    seen, out = set(), []
    for r in roots:
        r = os.path.normpath(r)
        if r.lower() not in seen and os.path.isdir(r):
            seen.add(r.lower())
            out.append(r)
    return out


def _looks_like_exe(name, lows):
    if not name.lower().endswith(".exe"):
        return False
    if any(t in name.lower() for t in _SKIP_TOKENS):
        return False
    return any(h in name.lower() for h in lows)


def _exe_in(root, lows, depth=2):
    """在目录内找出名字匹配线索的 exe。"""
    root = os.path.normpath(root)
    for cur, dirs, files in os.walk(root):
        if cur[len(root):].count(os.sep) >= depth:
            dirs[:] = []
        for f in files:
            if _looks_like_exe(f, lows):
                return os.path.join(cur, f)
    return None


def _from_common_dirs(proc, hints):
    """按目录名线索逐层剪枝搜索，避免全盘遍历。

    安装目录常见两种形态：%LOCALAPPDATA%\\Programs\\<产品名>，或 <盘符>\\<厂商目录>\\<产品名>，
    所以往下找两层。
    """
    lows = [h.lower() for h in hints]
    for root in _search_roots():
        for path in _iter_matches(root, lows, levels=2):
            if os.path.isdir(path):
                hit = _exe_in(path, lows, depth=1)
                if hit:
                    return hit
            elif _looks_like_exe(os.path.basename(path), lows):
                return path
    return None


def _iter_matches(root, lows, levels):
    """产出 root 下名字命中线索的条目；名字不沾边的目录继续往下找一层。"""
    frontier = [root]
    visited = 0
    for _ in range(levels):
        nxt = []
        for base in frontier:
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for entry in entries:
                visited += 1
                if visited > _MAX_VISITED:
                    return
                low = entry.name.lower()
                if any(h in low or low in h for h in lows):
                    yield entry.path
                    continue
                try:
                    if entry.is_dir():
                        nxt.append(entry.path)
                except OSError:
                    continue
        frontier = nxt


# --------------------------------------------------------------------------- 开始菜单
def _lnk_candidates(hints):
    lows = [h.lower() for h in hints]
    dirs = []
    for var, tail in (("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                      ("ProgramData", r"Microsoft\Windows\Start Menu\Programs"),
                      ("APPDATA", r"Microsoft\Windows\Start Menu\Programs\Startup")):
        base = os.environ.get(var)
        if base:
            dirs.append(os.path.join(base, tail))
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for cur, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".lnk") and any(h in f.lower() for h in lows):
                    out.append(os.path.join(cur, f))
    return out


def _from_start_menu(proc, hints):
    """用 WScript.Shell 解析 .lnk 的真实目标，不额外引入 pywin32 依赖。"""
    links = _lnk_candidates(hints)
    if not links:
        return None
    shell = "$s=New-Object -ComObject WScript.Shell;"
    body = ";".join("$s.CreateShortcut('%s').TargetPath" % p.replace("'", "''") for p in links)
    try:
        proc_obj = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", shell + body],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=CREATE_NO_WINDOW, universal_newlines=True)
    except Exception:
        return None
    for line in (proc_obj.stdout or "").splitlines():
        line = line.strip()
        if line.lower().endswith(".exe") and os.path.isfile(line):
            return line
    return None
