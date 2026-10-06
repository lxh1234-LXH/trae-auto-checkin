# -*- coding: utf-8 -*-
"""TRAEWork / WorkBuddy 每日自动签到。

无参数双击运行   打开设置界面
--run            执行一次签到（计划任务调用）
--only 名称      只跑某个应用，调试用
--detect         只做路径探测并打印结果
--install        按 config.json 里的时间注册计划任务
--uninstall      删除计划任务
--status         查看今天的状态
--calibrate 名称 打开校准向导，现场裁剪模板图
"""

import argparse
import ctypes
import os
import subprocess
import sys
import time

import core
import locate
import task
from core import log


# --------------------------------------------------------------------------- 路径解析
def resolve_exe(cfg, app):
    """取应用的可执行文件路径：配置优先，其次自动探测，探测结果回写配置。"""
    name = app["name"]
    slot = core.app_settings(cfg, name)
    manual = slot.get("exe") or ""
    if manual and os.path.isfile(manual):
        return manual

    exe, source = locate.find_exe(app["proc"], app.get("exe_hint", []), manual)
    if exe:
        slot["exe"] = exe
        core.save_config(cfg)
        log("%s：自动定位成功 %s（来源：%s）" % (name, exe, source))
    else:
        log("%s：未能定位程序，请在设置界面手动选择路径" % name)
    return exe


# --------------------------------------------------------------------------- 单应用流程
def run_app(app, exe):
    """执行一个应用的全部步骤，返回 (状态, 原因, 失败的模板名列表)。

    状态取值：ok 走完全部步骤 / skip 检测到今日已完成 / fail 出错
    """
    name = app["name"]
    failed = []

    def finish(status, reason=""):
        if status != "ok":
            shot = core.save_shot(name, status)
            if shot:
                log("%s：截图 %s" % (name, os.path.basename(shot)))
        return status, reason, failed

    hwnd = core.find_window(app["proc"], app["title_re"])
    if not hwnd:
        if not exe:
            return finish("fail", "未找到程序，且没有可用的启动路径")
        log("%s：未检测到进程，正在启动…" % name)
        try:
            subprocess.Popen([exe])
        except Exception as e:
            return finish("fail", "启动失败：%s" % e)
        deadline = time.time() + app.get("launch_wait", 30)
        while time.time() < deadline and not hwnd:
            time.sleep(2)
            hwnd = core.find_window(app["proc"], app["title_re"])
        if not hwnd:
            return finish("fail", "等待窗口超时")

    if not core.activate(hwnd):
        return finish("fail", "窗口切换前台失败（可能有全屏程序占用）")

    for step in app["steps"]:
        action = step["action"]

        if action in ("click_rel", "click_image", "skip_if_image", "open_menu"):
            core.ensure_active(hwnd)

        if action == "wait":
            time.sleep(step.get("seconds", 1))

        elif action == "activate":
            core.activate(hwnd)

        elif action == "key":
            if step.get("key", "").lower() == "esc":
                core.press_esc()

        elif action == "click_rel":
            rect = core.window_rect(hwnd)
            core.click_in_app(hwnd, rect.left + step["x"], rect.bottom + step["y"])

        elif action == "open_menu":
            # 账号菜单是点击切换的：可能已开也可能已关，因此点一次检测一次
            click_to, markers = step["click"], step["markers"]
            threshold = step.get("threshold", 0.78)
            hit = None
            for _ in range(step.get("tries", 3)):
                rect = core.window_rect(hwnd)
                core.click_in_app(hwnd, rect.left + click_to["x"], rect.bottom + click_to["y"])
                time.sleep(step.get("wait", 1.8))
                for marker in markers:
                    hit = core.find_template(marker, threshold)
                    if hit:
                        break
                if hit:
                    break
            if not hit:
                failed.extend(markers)
                return finish("fail", "未能打开账号菜单")

        elif action == "skip_if_image":
            hit = core.find_template(step["template"], step.get("threshold", 0.8))
            if hit:
                log("%s：%s（相似度 %.2f），跳过" % (name, step.get("reason", "已处理"), hit[2]))
                return finish("skip")

        elif action == "click_image":
            template = step["template"]
            hit = core.find_template(template, step.get("threshold", 0.8))
            if hit:
                core.click_in_app(hwnd, hit[0] + step.get("dx", 0), hit[1] + step.get("dy", 0))
                log("%s：已点击 %s（相似度 %.2f，尺度 %.2f）"
                    % (name, template, hit[2], hit[3]))
            elif step.get("fallback"):
                rect = core.window_rect(hwnd)
                core.click_in_app(hwnd, rect.left + step["fallback"]["x"],
                                  rect.bottom + step["fallback"]["y"])
                log("%s：未匹配到模板，使用备用坐标点击" % name)
            elif step.get("on_missing") == "skip":
                log("%s：%s" % (name, step.get("reason", "未找到目标，跳过")))
                return finish("skip")
            else:
                failed.append(template)
                return finish("fail", "未匹配到 %s" % template)

        elif action == "shot":
            shot = core.save_shot(name)
            if shot:
                log("%s：截图 %s" % (name, os.path.basename(shot)))

        else:
            log("%s：未知动作 %s" % (name, action))

    return "ok", "", failed


# --------------------------------------------------------------------------- 执行一次
def run_once(only=None):
    cfg = core.load_config()
    flows = core.load_flows()

    missing = core.missing_templates()
    if missing:
        log("以下模板缺失，相关步骤会失败，可在设置界面点「校准」补齐：%s" % "、".join(missing))

    state = core.load_state()
    if cfg.get("lock_guard", True) and core.is_locked():
        log("屏幕已锁定，本次跳过（计划任务会在解锁后补跑）")
        return 3
    state["issues"] = {}  # 每次执行都重新评估，避免上一轮的失败记录一直挂在界面上

    for app in flows.get("apps", []):
        name = app["name"]
        if only and name != only:
            continue

        slot = core.app_settings(cfg, name)
        if not slot.get("enabled", True):
            log("%s：已在设置中禁用，跳过" % name)
            continue
        if name in state["done"]:
            log("%s：今日已完成，跳过" % name)
            continue

        exe = resolve_exe(cfg, app)
        log("%s：开始处理" % name)
        status, reason, failed = run_app(app, exe)

        state["detail"][name] = status
        if reason:
            log("%s：结果 %s（%s）" % (name, status, reason))
        else:
            log("%s：结果 %s" % (name, status))

        if failed:
            state["issues"] = {t: reason or status for t in failed}
        if status in ("ok", "skip"):
            state["done"].append(name)
        core.save_state(state)

    log("本次执行结束：%s" % ("、".join(state["done"]) or "无"))
    return 0


# --------------------------------------------------------------------------- 探测
def detect():
    cfg = core.load_config()
    flows = core.load_flows()
    lines = []
    for app in flows.get("apps", []):
        name = app["name"]
        slot = core.app_settings(cfg, name)
        exe, source = locate.find_exe(app["proc"], app.get("exe_hint", []), slot.get("exe") or "")
        if exe:
            slot["exe"] = exe
            lines.append("%s：%s（来源：%s）" % (name, exe, source))
        else:
            lines.append("%s：未找到，请在设置界面手动指定" % name)
    core.save_config(cfg)
    for line in lines:
        log(line)
    missing = core.missing_templates()
    log("模板检查：%s" % ("全部就绪" if not missing else "缺少 " + "、".join(missing)))
    return 0


# --------------------------------------------------------------------------- 入口
def main():
    parser = argparse.ArgumentParser(description="TRAEWork / WorkBuddy 每日自动签到")
    parser.add_argument("--run", action="store_true", help="执行一次签到")
    parser.add_argument("--gui", action="store_true", help="打开设置界面")
    parser.add_argument("--only", help="只运行指定应用")
    parser.add_argument("--detect", action="store_true", help="只做路径探测")
    parser.add_argument("--install", action="store_true", help="注册计划任务")
    parser.add_argument("--uninstall", action="store_true", help="删除计划任务")
    parser.add_argument("--status", action="store_true", help="查看今天的状态")
    parser.add_argument("--calibrate", nargs="?", const="", help="校准指定模板")
    args = parser.parse_args()

    core.ensure_runtime_files()

    if args.status:
        state = core.load_state()
        log("日期 %s，已完成：%s" % (state["date"], "、".join(state["done"]) or "无"))
        for key, value in state["detail"].items():
            log("  %s -> %s" % (key, value))
        for template, reason in state.get("issues", {}).items():
            log("  待校准模板 %s：%s" % (template, reason))
        return 0

    if args.install:
        ok, message = task.install(core.load_config())
        if ok:
            schedule = core.load_config()["schedule"]
            log("计划任务已注册：%s（每天 %s 起，每 %s 分钟重试一次，持续 %s 小时）"
                % (core.TASK_NAME, schedule["time"], schedule["retry_interval_min"],
                   schedule["retry_duration_hours"]))
        else:
            log("计划任务注册失败：%s" % message)
        return 0 if ok else 1

    if args.uninstall:
        ok, message = task.uninstall()
        log("计划任务已删除" if ok else "删除失败：%s" % message)
        return 0 if ok else 1

    if args.detect:
        return detect()

    if args.calibrate is not None:
        import gui
        return gui.calibrate_cli(args.calibrate)

    if args.run or args.only:
        return run_once(args.only)

    if args.gui or getattr(sys, "frozen", False):
        import gui
        return gui.main()

    parser.print_help()
    return 0


def _guard():
    """打包成 --windowed 后没有控制台，异常必须自己记录并弹窗，否则会静默退出。"""
    try:
        return main()
    except Exception:
        import traceback
        detail = traceback.format_exc()
        log("未捕获异常：\n%s" % detail)
        try:
            ctypes.windll.user32.MessageBoxW(0, detail[-1500:], "trae-auto-checkin 出错", 0x10)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_guard())
