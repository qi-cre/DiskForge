"""磁盘健康预检与只读检查。"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass

from .fs import get_mount_info_linux, human_bytes


@dataclass
class DiskIssue:
    level: str        # 'error' | 'warning' | 'info'
    source: str       # 'mount' | 'smart' | 'dmesg' | 'access' | 'fs' | 'volume'
    message: str
    can_check: bool = False
    check_label: str = ""


# ---------------- Linux ----------------

def _check_smart_linux(device: str):
    if not device.startswith("/dev/"):
        return "unavailable", "不是块设备"
    if not shutil.which("smartctl"):
        return "unavailable", "smartctl 未安装"
    try:
        r = subprocess.run(
            ["smartctl", "-H", device],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return "unknown", f"执行失败: {e}"
    out = (r.stdout + r.stderr).lower()
    if "permission denied" in out or "operation not permitted" in out:
        return "unavailable", "需要 root 权限"
    if "health status: ok" in out or "passed" in out:
        return "ok", ""
    if "failed" in out or "bad" in out:
        return "fail", (r.stdout or r.stderr).strip()[:200]
    return "unknown", (r.stdout or r.stderr).strip()[:200]


def _check_dmesg_linux():
    if not shutil.which("dmesg"):
        return []
    try:
        r = subprocess.run(
            ["dmesg", "--level=err,warn"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    keywords = (
        "i/o error", "ata error", "medium error", "bad sector",
        "read error", "write error", "uncorrectable",
        "failed command", "reset failed",
    )
    hits = []
    for line in r.stdout.splitlines():
        low = line.lower()
        if any(k in low for k in keywords):
            hits.append(line.strip())
    return hits[-5:]


# ---------------- Windows ----------------

def _check_windows_volume(drive: str):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$v = Get-Volume -DriveLetter {drive[0]}; "
             f"if ($v) {{ \"$($v.HealthStatus)|$($v.FileSystemType)\" }}"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None, None
    out = r.stdout.strip()
    if "|" in out:
        health, fs = out.split("|", 1)
        return health.strip(), fs.strip()
    return None, None


# ---------------- 主入口 ----------------

def check_disk_health(path: str) -> list[DiskIssue]:
    issues: list[DiskIssue] = []
    system = platform.system()

    if not os.access(path, os.W_OK | os.X_OK):
        issues.append(DiskIssue(
            "error", "access",
            "目标目录不可写或不可进入，无法进行填充测试。",
        ))

    if system == "Linux":
        info = get_mount_info_linux(path)
        if info:
            device, mount, fstype, opts = info
            if "ro" in opts:
                issues.append(DiskIssue(
                    "error", "mount",
                    f"挂载选项含 ro：{fstype} 已只读挂载于 {mount}。",
                ))
            for opt in opts:
                if opt.startswith("errors="):
                    mode = opt.split("=", 1)[1]
                    if mode in ("remount-ro", "panic"):
                        issues.append(DiskIssue(
                            "warning", "mount",
                            f"挂载策略 {opt}：出现写入错误时会"
                            + ("重挂为只读" if mode == "remount-ro"
                               else "触发内核 panic"),
                        ))
            status, detail = _check_smart_linux(device)
            if status == "fail":
                issues.append(DiskIssue(
                    "error", "smart",
                    f"SMART 健康状态异常（{device}）：{detail[:200]}",
                    can_check=True,
                    check_label=f"smartctl -a {device}",
                ))
            elif status == "unknown":
                issues.append(DiskIssue(
                    "warning", "smart",
                    f"无法确认 SMART 健康状态（{device}）：{detail[:120]}",
                    can_check=True,
                    check_label=f"smartctl -a {device}",
                ))
            err_lines = _check_dmesg_linux()
            if err_lines:
                issues.append(DiskIssue(
                    "warning", "dmesg",
                    "内核日志中发现最近的磁盘相关错误：\n  "
                    + "\n  ".join(err_lines[:3]),
                ))
        try:
            du = shutil.disk_usage(path)
            if du.total and du.free < du.total * 0.01:
                issues.append(DiskIssue(
                    "warning", "fs",
                    f"剩余空间极低：{human_bytes(du.free)}（<1%）。",
                ))
        except OSError:
            pass

    elif system == "Windows":
        drive, _ = os.path.splitdrive(os.path.abspath(path))
        if drive:
            health, fs = _check_windows_volume(drive)
            if health and health.lower() not in ("healthy", "ok"):
                issues.append(DiskIssue(
                    "error", "volume",
                    f"卷 {drive} 健康状态：{health}（{fs}）。",
                    can_check=True,
                    check_label=f"chkdsk {drive} /scan",
                ))

    elif system == "Darwin":
        try:
            r = subprocess.run(
                ["diskutil", "info", "-plist", path],
                capture_output=True, text=True, timeout=10,
            )
            out = r.stdout.lower()
            if "<key>readonlyvolume</key>" in out and "<true/>" in out:
                issues.append(DiskIssue(
                    "error", "mount", "卷已只读挂载。",
                ))
        except Exception:
            pass

    return issues


def run_issue_check(target: str, issue: DiskIssue) -> str:
    """只读运行某个检查，返回可读输出。"""
    system = platform.system()

    if issue.source == "smart" and system == "Linux":
        info = get_mount_info_linux(target)
        if not info:
            return "无法定位块设备。"
        device = info[0]
        if not shutil.which("smartctl"):
            return "smartctl 未安装。"
        try:
            r = subprocess.run(
                ["smartctl", "-a", device],
                capture_output=True, text=True, timeout=30,
            )
            return (r.stdout or r.stderr)[:4000]
        except Exception as e:
            return f"执行失败：{e}"

    if issue.source == "volume" and system == "Windows":
        drive, _ = os.path.splitdrive(os.path.abspath(target))
        if drive:
            try:
                r = subprocess.run(
                    ["chkdsk", drive, "/scan"],
                    capture_output=True, text=True, timeout=600,
                )
                return (r.stdout or r.stderr)[:4000]
            except Exception as e:
                return f"执行失败：{e}"

    return "该问题没有可自动运行的检查。"