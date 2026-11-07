"""文件系统识别、路径与通用工具。"""

from __future__ import annotations

import errno
import os
import platform
import re
import subprocess
import sys

MB = 1024 * 1024
GiB = 1024 ** 3
TiB = 1024 ** 4

FS_MAX_FILE_SIZE = {
    "vfat":  4 * GiB - 1,
    "fat":   4 * GiB - 1,
    "fat12": 4 * GiB - 1,
    "fat16": 4 * GiB - 1,
    "fat32": 4 * GiB - 1,
    "msdos": 4 * GiB - 1,
}
DEFAULT_MAX_FILE_SIZE = 64 * TiB

NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smb", "smb2", "smb3",
    "smbfs", "afpfs", "fuse.sshfs",
}
MEMORY_FS = {"tmpfs", "ramfs"}
UNION_FS = {"overlay", "overlayfs", "aufs"}
FAT_FS = {"vfat", "fat", "fat12", "fat16", "fat32", "msdos"}


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} EiB"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def is_write_blocking_err(e: OSError) -> bool:
    """磁盘满 / 配额 / 文件过大，都视为停止信号。"""
    return e.errno in (
        errno.ENOSPC,
        getattr(errno, "EDQUOT", -1),
        getattr(errno, "EFBIG", -1),
    )


def long_path(p: str) -> str:
    """Windows 上超过 MAX_PATH 时自动加 \\\\?\\ 前缀。"""
    if sys.platform != "win32":
        return p
    if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        return p
    p = os.path.abspath(p)
    if len(p) < 248:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


# ---------------- 文件系统识别 ----------------

def _fs_linux(path: str) -> str:
    best_mount, best_fs = "", "unknown"
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                fstype = parts[2].lower()
                if "." in fstype:
                    fstype = fstype.split(".", 1)[0]
                if path == mount or path.startswith(mount.rstrip("/") + "/"):
                    if len(mount) > len(best_mount):
                        best_mount, best_fs = mount, fstype
    except OSError:
        pass
    return best_fs


_MOUNT_RE = re.compile(r" on (.+?) \(([^,)]+)")


def _fs_darwin(path: str) -> str:
    try:
        out = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return "unknown"
    best_mount, best_fs = "", "unknown"
    for line in out.splitlines():
        m = _MOUNT_RE.search(line)
        if not m:
            continue
        mount, fstype = m.group(1), m.group(2).strip().lower()
        if path == mount or path.startswith(mount.rstrip("/") + "/"):
            if len(mount) > len(best_mount):
                best_mount, best_fs = mount, fstype
    return best_fs


def _fs_windows(path: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return "unknown"
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if not drive:
        return "unknown"
    root = drive + "\\"
    buf = ctypes.create_unicode_buffer(261)
    fn = ctypes.windll.kernel32.GetVolumeInformationW
    fn.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    fn.restype = wintypes.BOOL
    if fn(root, None, 0, None, None, None, buf, len(buf)):
        return buf.value.lower() or "unknown"
    return "unknown"


def detect_fs(path: str) -> str:
    try:
        real = os.path.realpath(os.path.abspath(path))
    except OSError:
        real = path
    system = platform.system()
    try:
        if system == "Linux":
            return _fs_linux(real)
        if system == "Darwin":
            return _fs_darwin(real)
        if system == "Windows":
            return _fs_windows(real)
    except Exception:
        pass
    return "unknown"


def max_file_size_for(fs_type: str) -> int:
    return FS_MAX_FILE_SIZE.get(fs_type.lower(), DEFAULT_MAX_FILE_SIZE)


def fs_warnings(fs_type: str) -> list[str]:
    fs = fs_type.lower()
    out: list[str] = []
    if fs in NETWORK_FS:
        out.append(
            f"[yellow]网络文件系统 ({fs})[/yellow]：空闲空间来自远端，"
            "可能不准确；fsync 延迟大。建议 fsync_every 设大或设 0。"
        )
    if fs in MEMORY_FS:
        out.append(
            f"[red]内存文件系统 ({fs})[/red]：填充会吃掉 RAM，"
            "可能让系统变慢甚至 OOM。"
        )
    if fs in UNION_FS:
        out.append(
            f"[yellow]联合文件系统 ({fs})[/yellow]：写入落到 upperdir，"
            "free 统计可能与真实物理空间不一致。"
        )
    if fs in FAT_FS:
        out.append(
            f"[yellow]FAT 系列 ({fs})[/yellow]：单文件最大 4GiB-1，"
            "工具会自动轮转新文件。"
        )
    if fs == "unknown":
        out.append("[dim]未能识别文件系统类型，将使用通用策略。[/dim]")
    return out


# ---------------- mount 信息（Linux） ----------------

def get_mount_info_linux(path: str):
    """返回 (device, mountpoint, fstype, options_list) 或 None。"""
    best = None
    best_len = -1
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                dev, mnt, fstype, opts = parts[0], parts[1], parts[2], parts[3]
                mnt = mnt.replace("\\040", " ")
                if path == mnt or path.startswith(mnt.rstrip("/") + "/"):
                    if len(mnt) > best_len:
                        best = (dev, mnt, fstype, opts.split(","))
                        best_len = len(mnt)
    except OSError:
        pass
    return best