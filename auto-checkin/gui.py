# -*- coding: utf-8 -*-
"""图形界面：设置窗口 + 模板校准向导。

设置窗口让使用者自己填签到时间、重试参数、应用路径；校准向导用全屏截图 + 拖框裁剪，
现场生成适配本机分辨率/主题的模板图，解决换台电脑就匹配不上的问题。
"""

import os
import re
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

import core
import locate
import task

CREATE_NO_WINDOW = 0x08000000

TEMPLATE_LABELS = {
    "trae_label.png": "TRAEWork 账号菜单里的「签到」入口",
    "trae_signed.png": "TRAEWork 里「今日已签到」的标记",
    "wb_claim.png": "WorkBuddy 的「领取」按钮",
    "wb_claimed.png": "WorkBuddy 里「今日已领取」的标记",
}

MIN_CROP = 8


def collect_templates():
    """按 flows.json 中出现的顺序收集全部模板名。"""
    names = []
    for app in core.load_flows().get("apps", []):
        for step in app.get("steps", []):
            if step.get("template"):
                names.append(step["template"])
            names.extend(step.get("markers") or [])
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _spawn_run():
    """后台静默跑一次签到，不阻塞界面。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--run"]
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        runner = pythonw if os.path.exists(pythonw) else sys.executable
        cmd = [runner, os.path.join(core.APP_DIR, "checkin.py"), "--run"]
    subprocess.Popen(cmd, cwd=core.APP_DIR, creationflags=CREATE_NO_WINDOW)


def _open_path(path):
    if os.path.exists(path):
        os.startfile(path)
    else:
        messagebox.showinfo("提示", "文件还不存在：\n%s" % path)


# --------------------------------------------------------------------------- 校准向导
class CalibrateOverlay(tk.Toplevel):
    """全屏显示刚截下的画面，拖动鼠标框选按钮区域，裁剪保存为模板图。"""

    def __init__(self, master, image, template_name, on_done=None):
        tk.Toplevel.__init__(self, master)
        self.image = image
        self.template_name = template_name
        self.on_done = on_done
        self.origin = None
        self.rect_id = None

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry("%dx%d+0+0" % (image.width, image.height))

        self.canvas = tk.Canvas(self, width=image.width, height=image.height,
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_text(
            image.width // 2, 40,
            text="拖动鼠标框选【%s】所在的按钮区域    Esc 取消"
                 % TEMPLATE_LABELS.get(template_name, template_name),
            fill="#ffffff", font=("Microsoft YaHei", 18, "bold"))
        self.canvas.create_rectangle(0, 60, image.width, 100,
                                     fill="#000000", outline="", stipple="gray50")

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _e: self._cancel())

        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _press(self, event):
        self.origin = (event.x, event.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#22d3ee", width=3)

    def _drag(self, event):
        if self.origin and self.rect_id:
            self.canvas.coords(self.rect_id, self.origin[0], self.origin[1], event.x, event.y)

    def _release(self, event):
        if not self.origin:
            return
        x1, y1 = self.origin
        x2, y2 = event.x, event.y
        box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self.origin = None
        if box[2] - box[0] < MIN_CROP or box[3] - box[1] < MIN_CROP:
            messagebox.showwarning("范围太小", "框选区域太小，请重新拖动一次。")
            return

        os.makedirs(core.TEMPLATE_DIR, exist_ok=True)
        target = os.path.join(core.TEMPLATE_DIR, self.template_name)
        self.image.crop(box).save(target)
        self._close()
        messagebox.showinfo("校准完成",
                            "%s 已更新（%d x %d）。\n下次执行就会用这张新模板匹配。"
                            % (self.template_name, box[2] - box[0], box[3] - box[1]))
        if self.on_done:
            self.on_done()

    def _cancel(self):
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# --------------------------------------------------------------------------- 设置窗口
class SettingsWindow(object):
    def __init__(self, root):
        self.root = root
        self.cfg = core.load_config()
        self.flows = core.load_flows()
        self.exe_vars = {}
        self.enabled_vars = {}
        self.app_status = {}
        self.tpl_status = {}

        root.title("trae-auto-checkin 设置")
        root.resizable(False, False)

        schedule = self.cfg["schedule"]
        self.time_var = tk.StringVar(value=schedule.get("time", "08:00"))
        self.interval_var = tk.StringVar(value=str(schedule.get("retry_interval_min", 30)))
        self.duration_var = tk.StringVar(value=str(schedule.get("retry_duration_hours", 16)))
        self.logon_var = tk.BooleanVar(value=schedule.get("on_logon", True))
        self.lock_var = tk.BooleanVar(value=self.cfg.get("lock_guard", True))

        self._build_plan()
        self._build_apps()
        self._build_templates()
        self._build_actions()
        self._refresh_status()
        self._center()

    # ---------------------------------------------------------------- 布局
    def _center(self):
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry("+%d+%d" % (x, y))

    def _section(self, title, row):
        frame = ttk.LabelFrame(self.root, text=title, padding=10)
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(6, 0))
        self.root.columnconfigure(0, weight=1)
        return frame

    def _build_plan(self):
        frame = self._section("签到计划", 0)
        ttk.Label(frame, text="每天").grid(row=0, column=0)
        ttk.Entry(frame, textvariable=self.time_var, width=7).grid(row=0, column=1, padx=4)
        ttk.Label(frame, text="开始（24 小时制 HH:MM）   重试间隔").grid(row=0, column=2)
        ttk.Entry(frame, textvariable=self.interval_var, width=5).grid(row=0, column=3, padx=4)
        ttk.Label(frame, text="分钟，持续").grid(row=0, column=4)
        ttk.Entry(frame, textvariable=self.duration_var, width=5).grid(row=0, column=5, padx=4)
        ttk.Label(frame, text="小时").grid(row=0, column=6)

        ttk.Checkbutton(frame, text="登录时也补跑一次（开机当天不漏签）",
                        variable=self.logon_var).grid(row=1, column=0, columnspan=4,
                                                      sticky="w", pady=(8, 0))
        ttk.Checkbutton(frame, text="锁屏时不操作，等解锁后重试",
                        variable=self.lock_var).grid(row=1, column=4, columnspan=3,
                                                     sticky="w", pady=(8, 0))

    def _build_apps(self):
        frame = self._section("应用与路径", 1)
        for i, app in enumerate(self.flows.get("apps", [])):
            name = app["name"]
            slot = core.app_settings(self.cfg, name)
            self.enabled_vars[name] = tk.BooleanVar(value=slot.get("enabled", True))
            self.exe_vars[name] = tk.StringVar(value=slot.get("exe") or "")

            ttk.Checkbutton(frame, text=name, variable=self.enabled_vars[name]).grid(
                row=i * 2, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=self.exe_vars[name], width=58)
            entry.grid(row=i * 2 + 1, column=0, columnspan=2, sticky="we", pady=(2, 0))
            ttk.Button(frame, text="浏览", width=6,
                       command=lambda n=name: self._browse(n)).grid(row=i * 2 + 1, column=2, padx=4)
            ttk.Button(frame, text="自动检测", width=8,
                       command=lambda n=name: self._detect(n)).grid(row=i * 2 + 1, column=3)
            label = ttk.Label(frame, text="")
            label.grid(row=i * 2, column=2, columnspan=2, sticky="e")
            self.app_status[name] = label

    def _build_templates(self):
        frame = self._section("识别模板（匹配不上时点「校准」现场框选）", 2)
        for i, name in enumerate(collect_templates()):
            ttk.Label(frame, text=TEMPLATE_LABELS.get(name, name)).grid(
                row=i, column=0, sticky="w")
            ttk.Label(frame, text=name, foreground="#888888").grid(
                row=i, column=1, sticky="w", padx=8)
            label = ttk.Label(frame, text="")
            label.grid(row=i, column=2, sticky="w", padx=8)
            self.tpl_status[name] = label
            ttk.Button(frame, text="校准", width=6,
                       command=lambda n=name: self._calibrate(n)).grid(row=i, column=3)

    def _build_actions(self):
        frame = ttk.Frame(self.root)
        frame.grid(row=3, column=0, sticky="ew", padx=12, pady=10)
        ttk.Button(frame, text="保存", command=self._save).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(frame, text="立即运行一次", command=self._run_now).grid(row=0, column=1, padx=6)
        ttk.Button(frame, text="注册计划任务", command=self._install).grid(row=0, column=2, padx=6)
        ttk.Button(frame, text="删除计划任务", command=self._uninstall).grid(row=0, column=3, padx=6)
        ttk.Button(frame, text="查看日志", command=lambda: _open_path(core.LOG_FILE)).grid(
            row=0, column=4, padx=6)
        ttk.Button(frame, text="配置目录", command=lambda: _open_path(core.DATA_DIR)).grid(
            row=0, column=5, padx=6)

    # ---------------------------------------------------------------- 状态刷新
    def _refresh_status(self):
        for name, path in self.exe_vars.items():
            label = self.app_status[name]
            value = path.get().strip()
            if value and os.path.isfile(value):
                label.config(text="已就绪", foreground="#16a34a")
            else:
                label.config(text="未找到，请点「自动检测」或手动选择", foreground="#dc2626")

        issues = core.load_state().get("issues", {})
        for name, label in self.tpl_status.items():
            if not core.template_file(name):
                label.config(text="缺失", foreground="#dc2626")
            elif name in issues:
                label.config(text="上次匹配失败", foreground="#dc2626")
            else:
                label.config(text="就绪", foreground="#16a34a")

    # ---------------------------------------------------------------- 交互
    def _browse(self, name):
        path = filedialog.askopenfilename(title="选择 %s 的可执行文件" % name,
                                          filetypes=[("可执行文件", "*.exe")])
        if path:
            self.exe_vars[name].set(path)
            self._refresh_status()

    def _detect(self, name):
        app = next((a for a in self.flows.get("apps", []) if a["name"] == name), None)
        if not app:
            return
        self.root.config(cursor="watch")
        self.root.update()
        try:
            exe, source = locate.find_exe(app["proc"], app.get("exe_hint", []),
                                          self.exe_vars[name].get().strip())
        finally:
            self.root.config(cursor="")
        if exe:
            self.exe_vars[name].set(exe)
            messagebox.showinfo("检测成功", "%s\n\n来源：%s" % (exe, source))
        else:
            messagebox.showwarning("未检测到",
                                   "没能自动找到 %s。\n\n请点「浏览」手动选择它的 exe，"
                                   "一般是安装目录里名字带 %s 的那个程序。"
                                   % (name, "、".join(app.get("exe_hint", []))))
        self._refresh_status()

    def _calibrate(self, name):
        label = TEMPLATE_LABELS.get(name, name)
        if not messagebox.askokcancel(
                "校准模板",
                "即将校准：%s\n\n请先把目标界面调到能看到【%s】的状态，"
                "然后点「确定」。\n点确定后 5 秒自动截屏。" % (name, label)):
            return
        self.root.iconify()
        self.root.after(5000, lambda: self._capture(name))

    def _capture(self, name):
        try:
            image = core.grab_screen()
        except Exception as e:
            messagebox.showerror("截屏失败", str(e))
            return
        CalibrateOverlay(self.root, image, name, on_done=self._after_calibrate)

    def _after_calibrate(self):
        self.root.deiconify()
        self._refresh_status()

    def _save(self):
        if not re.match(r"^\d{1,2}:\d{2}$", self.time_var.get().strip()):
            messagebox.showerror("格式不对", "签到时间请填 HH:MM，例如 09:00。")
            return False
        try:
            interval = int(self.interval_var.get())
            duration = int(self.duration_var.get())
        except ValueError:
            messagebox.showerror("格式不对", "重试间隔/持续时长请填整数。")
            return False
        if not 0 <= interval <= 1440 or not 1 <= duration <= 24:
            messagebox.showerror("数值超范围", "重试间隔填 0~1440 分钟，持续时长填 1~24 小时。")
            return False

        hour, minute = [int(x) for x in self.time_var.get().strip().split(":")]
        if not (0 <= hour < 24 and 0 <= minute < 60):
            messagebox.showerror("时间不合理", "小时是 0~23，分钟是 0~59。")
            return False

        self.cfg["schedule"] = {
            "time": "%02d:%02d" % (hour, minute),
            "retry_interval_min": interval,
            "retry_duration_hours": duration,
            "on_logon": bool(self.logon_var.get()),
        }
        self.cfg["lock_guard"] = bool(self.lock_var.get())
        for name, var in self.enabled_vars.items():
            self.cfg["apps"].setdefault(name, {})["enabled"] = bool(var.get())
        for name, var in self.exe_vars.items():
            self.cfg["apps"].setdefault(name, {})["exe"] = var.get().strip()

        core.save_config(self.cfg)
        self._refresh_status()
        return True

    def _run_now(self):
        if not self._save():
            return
        _spawn_run()
        messagebox.showinfo("已在后台运行",
                            "签到已在后台执行。\n结果会记录到 checkin.log，"
                            "失败时会自动在 shots 目录留截图。")

    def _install(self):
        if not self._save():
            return
        ok, message = task.install(self.cfg)
        if ok:
            schedule = self.cfg["schedule"]
            messagebox.showinfo("注册成功",
                                "计划任务 %s 已注册。\n每天 %s 开始，每 %d 分钟重试一次，"
                                "持续 %d 小时。" % (core.TASK_NAME, schedule["time"],
                                                    schedule["retry_interval_min"],
                                                    schedule["retry_duration_hours"]))
        else:
            messagebox.showerror("注册失败", message or "schtasks 返回错误")

    def _uninstall(self):
        ok, message = task.uninstall()
        if ok:
            messagebox.showinfo("已删除", "计划任务 %s 已删除。" % core.TASK_NAME)
        else:
            messagebox.showerror("删除失败", message or "任务可能本来就不存在")


# --------------------------------------------------------------------------- 入口
def main():
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()
    return 0


def calibrate_cli(name):
    """命令行直接进校准：不给名字就列出模板状态。"""
    names = collect_templates()
    if not name:
        for n in names:
            state = "已就绪" if core.template_file(n) else "缺失"
            print("%-18s %-28s %s" % (n, TEMPLATE_LABELS.get(n, ""), state))
        return 0
    if name not in names:
        print("未知模板：%s，可选：%s" % (name, "、".join(names)))
        return 1

    root = tk.Tk()
    root.withdraw()
    root.after(3000, lambda: CalibrateOverlay(
        root, core.grab_screen(), name, on_done=root.destroy))
    root.mainloop()
    return 0
