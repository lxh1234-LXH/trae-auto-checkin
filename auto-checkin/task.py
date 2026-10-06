# -*- coding: utf-8 -*-
"""Windows 计划任务的注册 / 删除 / 查询。

签到时间、重试间隔、重试时长都来自 config.json，注册时写进任务 XML。
"""

import datetime
import os
import subprocess
import sys

import core

CREATE_NO_WINDOW = 0x08000000

XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>每天自动打开 TRAEWork / WorkBuddy 完成签到领取积分</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>%(start)s</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT%(interval)uM</Interval>
        <Duration>PT%(duration)uH</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>%(logon)s
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(command)s</Command>
      <Arguments>%(arguments)s</Arguments>
      <WorkingDirectory>%(workdir)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

LOGON_TRIGGER = """
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%(user)s</UserId>
    </LogonTrigger>"""


def _entry_point():
    """返回 (可执行文件, 参数)。打包后直接跑 exe，源码运行时跑 pythonw + checkin.py。"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--run"
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return pythonw, '"%s" --run' % os.path.join(core.APP_DIR, "checkin.py")


def _next_start(time_text):
    """今天还没到点就用今天，已经过了就用明天。"""
    try:
        hour, minute = [int(x) for x in time_text.split(":")]
    except Exception:
        hour, minute = 8, 0
    now = datetime.datetime.now()
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= now:
        moment += datetime.timedelta(days=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def build_xml(cfg):
    schedule = cfg.get("schedule", {})
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    command, arguments = _entry_point()
    logon = LOGON_TRIGGER % {"user": user} if schedule.get("on_logon", True) else ""
    return XML_TEMPLATE % {
        "start": _next_start(schedule.get("time", "08:00")),
        "interval": max(1, int(schedule.get("retry_interval_min", 30))),
        "duration": max(1, int(schedule.get("retry_duration_hours", 16))),
        "logon": logon,
        "user": user,
        "command": command,
        "arguments": arguments,
        "workdir": core.APP_DIR,
    }


def install(cfg):
    """注册计划任务，同时清理旧版本留下的任务名。返回 (成功, 说明)。"""
    for legacy in core.LEGACY_TASKS:
        subprocess.run(["schtasks", "/Delete", "/TN", legacy, "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW)

    xml_path = os.path.join(core.DATA_DIR, "task.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(build_xml(cfg))

    result = subprocess.run(["schtasks", "/Create", "/TN", core.TASK_NAME, "/XML", xml_path, "/F"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW, universal_newlines=True)
    if result.returncode != 0:
        return False, (result.stdout or "").strip()
    return True, ""


def uninstall():
    result = subprocess.run(["schtasks", "/Delete", "/TN", core.TASK_NAME, "/F"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW, universal_newlines=True)
    return result.returncode == 0, (result.stdout or "").strip()


def is_installed():
    result = subprocess.run(["schtasks", "/Query", "/TN", core.TASK_NAME],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW)
    return result.returncode == 0


def query_text():
    result = subprocess.run(["schtasks", "/Query", "/TN", core.TASK_NAME, "/V", "/FO", "LIST"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW, universal_newlines=True)
    return (result.stdout or "").strip()


def run_now():
    subprocess.run(["schtasks", "/Run", "/TN", core.TASK_NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=CREATE_NO_WINDOW)
