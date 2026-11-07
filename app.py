"""DiskForge 主界面。"""

from __future__ import annotations

import errno
import heapq
import os
import shutil
import struct
import threading
import time
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal
from textual.widgets import (
    Button, Checkbox, Footer, Header, Input, Label,
    RichLog, Select, Static,
)

from . import __app_name__, __version__
from .core import FillConfig, ScanResult
from .fs import (
    MB,
    detect_fs,
    fmt_duration,
    fs_warnings,
    human_bytes,
    is_write_blocking_err,
    long_path,
    max_file_size_for,
)
from .health import DiskIssue, check_disk_health, run_issue_check
from .widgets import DiskCheckScreen


class DiskForgeApp(App):
    CSS = """
    Screen { layout: vertical; }
    #config {
        height: auto; border: round $primary;
        padding: 1 2; margin: 0 1;
    }
    #config-grid {
        grid-size: 2 7; grid-columns: 22 1fr;
        grid-gutter: 0 2; height: auto;
    }
    #config-grid Label { padding-top: 1; }
    #opts { height: auto; }
    #opts Checkbox { margin: 0 2 0 0; height: 3; }
    #buttons { height: auto; align: center middle; padding: 1 0; }
    #buttons Button { margin: 0 2; min-width: 14; }
    #stats {
        height: auto; border: round $secondary;
        padding: 1 2; margin: 0 1;
    }
    #stats-text { height: auto; }
    #log {
        height: 1fr; border: round $accent;
        margin: 0 1 1 1;
    }
    """

    TITLE = __app_name__
    SUB_TITLE = f"v{__version__} — 写入速度 / 真实容量测试 (仅用于专用测试盘)"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("s", "start", "Start", show=False),
        Binding("x", "stop", "Stop", show=False),
        Binding("r", "scan", "Scan", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cancel_event = threading.Event()
        self._busy = False
        self._scan_result: Optional[ScanResult] = None
        self._pending_fill_cfg: Optional[FillConfig] = None
        self._scan_progress = {"dirs": 0, "files": 0, "size": 0}
        self._fill_state = {
            "total": 0, "speed": 0.0, "elapsed": 0.0,
            "files": 0, "current": 0, "rotations": 0,
        }

    # ------------------------------------------------------ 布局

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="config"):
            with Grid(id="config-grid"):
                yield Label("Target dir")
                yield Input(
                    placeholder="/mnt/test_usb  或  D:\\test", id="target"
                )
                yield Label("Block size (bytes)")
                yield Input(value=str(4 * MB), id="block-size")
                yield Label("Reserve (MiB)")
                yield Input(value="64", id="reserve")
                yield Label("Max files")
                yield Input(value="100000", id="max-files")
                yield Label("Mode")
                yield Select(
                    [
                        ("zero  (最快，只测速度)", "zero"),
                        ("index (块号，检测覆盖)", "index"),
                        ("random (随机模板，最严)", "random"),
                    ],
                    value="zero", id="mode", allow_blank=False,
                )
                yield Label("fsync every")
                yield Input(value="8", id="fsync")
                yield Label("Options")
                with Horizontal(id="opts"):
                    yield Checkbox("Verify", id="verify")
                    yield Checkbox("Keep files", id="keep")
        with Horizontal(id="buttons"):
            yield Button("Scan", id="scan", variant="primary")
            yield Button("Start", id="start", variant="success")
            yield Button("Stop", id="stop", variant="error", disabled=True)
        with Container(id="stats"):
            yield Static("Idle.", id="stats-text", markup=True)
        yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    # ------------------------------------------------------ 生命周期

    def on_mount(self) -> None:
        self._log(f"[bold]{__app_name__} v{__version__}[/bold] 启动。")
        self._log(
            "[yellow]警告：[/yellow]本工具会持续向目标目录写文件直到磁盘满。"
            "请只对专用测试分区/U盘使用。"
        )
        self._log(
            "建议：先点 [cyan]Scan[/cyan] 读取已有文件占用和文件系统类型，"
            "再 [cyan]Start[/cyan]。"
        )
        self._log(
            "[dim]读写前会自动做一次磁盘健康预检；"
            "发现问题会弹窗询问是否运行检查。[/dim]"
        )
        self._render_stats()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan":
            self.action_scan()
        elif event.button.id == "start":
            self.action_start()
        elif event.button.id == "stop":
            self.action_stop()

    # ------------------------------------------------------ actions

    def action_scan(self) -> None:
        if self._busy:
            self._log("[yellow]正在忙，稍后再试。[/yellow]")
            return
        try:
            target = self._read_target()
        except ValueError as e:
            self._log(f"[red]{e}[/red]")
            return
        self._pending_fill_cfg = None
        self._preflight(target, lambda: self._launch_scan(target))

    def action_start(self) -> None:
        if self._busy:
            self._log("[yellow]已在运行中。[/yellow]")
            return
        try:
            cfg = self._read_config()
        except ValueError as e:
            self._log(f"[red]配置错误：{e}[/red]")
            return

        need_scan = (
            self._scan_result is None
            or self._scan_result.target != cfg.target
        )

        def proceed():
            if need_scan:
                self._log("[cyan]尚未扫描该目录，自动先扫描…[/cyan]")
                self._pending_fill_cfg = cfg
                self._launch_scan(cfg.target)
            else:
                self._launch_fill(cfg)

        self._preflight(cfg.target, proceed)

    def action_stop(self) -> None:
        if not self._busy:
            return
        self._cancel_event.set()
        self._log("[yellow]已请求停止…[/yellow]")

    def action_quit(self) -> None:
        if self._busy:
            self._cancel_event.set()
        self.exit()

    # ------------------------------------------------------ 预检流程

    def _preflight(self, target: str, then) -> None:
        self._busy = True
        self.query_one("#scan", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self._log("[cyan]磁盘健康预检…[/cyan]")
        self._preflight_worker(target, then)

    @work(thread=True, exclusive=True, group="preflight")
    def _preflight_worker(self, target: str, then) -> None:
        try:
            issues = check_disk_health(target)
        except Exception as e:  # noqa: BLE001
            issues = [DiskIssue(
                "warning", "internal", f"预检发生异常：{e!r}"
            )]
        issues = [i for i in issues if i.level in ("error", "warning")]
        self._safe_call(self._show_preflight_result, target, issues, then)

    def _show_preflight_result(self, target, issues, then) -> None:
        self._busy = False

        if not issues:
            self._log("[green]磁盘健康预检通过。[/green]")
            self.query_one("#scan", Button).disabled = False
            self.query_one("#start", Button).disabled = False
            then()
            return

        self._log(
            f"[yellow]预检发现 {len(issues)} 个问题，等待用户确认…[/yellow]"
        )
        for i in issues:
            tag = {"error": "red", "warning": "yellow"}.get(i.level, "white")
            self._log(
                f"  [{tag}]{i.level}[/{tag}] ({i.source}) {i.message}"
            )

        def on_dismiss(result):
            if result == "run":
                self._run_selected_checks(target, issues, then)
            elif result == "skip":
                self._log("[yellow]已跳过磁盘检查，继续。[/yellow]")
                self.query_one("#scan", Button).disabled = False
                self.query_one("#start", Button).disabled = False
                then()
            else:
                self._log("[dim]已取消操作。[/dim]")
                self.query_one("#scan", Button).disabled = False
                self.query_one("#start", Button).disabled = False

        self.push_screen(DiskCheckScreen(issues, target), on_dismiss)

    # ------------------------------------------------------ 运行用户选定的检查

    def _run_selected_checks(self, target, issues, then) -> None:
        self._busy = True
        self.query_one("#scan", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self._log("[cyan]开始运行磁盘检查（只读）…[/cyan]")
        self._run_checks_worker(target, issues, then)

    @work(thread=True, exclusive=True, group="runcheck")
    def _run_checks_worker(self, target, issues, then) -> None:
        for issue in issues:
            if not issue.can_check:
                continue
            self._safe_call(
                self._log, f"[cyan]▸ {issue.check_label}[/cyan]"
            )
            try:
                output = run_issue_check(target, issue)
            except Exception as e:  # noqa: BLE001
                output = f"检查执行失败：{e!r}"
            for line in (output or "").splitlines():
                self._safe_call(self._log, f"  [dim]{line}[/dim]")

        self._safe_call(self._on_checks_finished, target, then)

    def _on_checks_finished(self, target, then) -> None:
        self._busy = False
        self.query_one("#scan", Button).disabled = False
        self.query_one("#start", Button).disabled = False
        self._log("[green]磁盘检查完成，继续操作。[/green]")
        then()

    # ------------------------------------------------------ 读取输入

    def _read_target(self) -> str:
        target = self.query_one("#target", Input).value.strip()
        if not target:
            raise ValueError("Target dir 不能为空")
        target = os.path.abspath(os.path.expanduser(target))
        if not os.path.isdir(target):
            raise ValueError(f"不是目录：{target}")
        return target

    def _read_config(self) -> FillConfig:
        target = self._read_target()
        block_size = int(self.query_one("#block-size", Input).value.strip())
        if block_size < 8:
            raise ValueError("Block size 至少为 8")
        reserve_mb = int(self.query_one("#reserve", Input).value.strip())
        if reserve_mb < 0:
            raise ValueError("Reserve 不能为负")
        max_files = int(self.query_one("#max-files", Input).value.strip())
        if max_files < 1:
            raise ValueError("Max files 至少为 1")
        fsync_every = int(self.query_one("#fsync", Input).value.strip())
        if fsync_every < 0:
            raise ValueError("fsync every 不能为负")
        mode = self.query_one("#mode", Select).value
        verify = self.query_one("#verify", Checkbox).value
        keep = self.query_one("#keep", Checkbox).value

        if (self._scan_result is not None
                and self._scan_result.target == target):
            fs_type = self._scan_result.fs_type
        else:
            fs_type = detect_fs(target)

        return FillConfig(
            target=target,
            block_size=block_size,
            reserve_bytes=reserve_mb * MB,
            max_files=max_files,
            mode=mode,                    # type: ignore[arg-type]
            fsync_every=fsync_every,
            verify=verify,                # type: ignore[arg-type]
            keep=keep,                    # type: ignore[arg-type]
            fs_type=fs_type,
            max_file_size=max_file_size_for(fs_type),
        )

    # ------------------------------------------------------ 启动 worker

    def _launch_scan(self, target: str) -> None:
        self._cancel_event.clear()
        self._busy = True
        self._scan_progress = {"dirs": 0, "files": 0, "size": 0}
        self.query_one("#scan", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self.query_one("#stop", Button).disabled = False
        self._log(f"[cyan]扫描目录：{target}[/cyan]")
        self._render_stats()
        self.run_scan(target)

    def _launch_fill(self, cfg: FillConfig) -> None:
        self._cancel_event.clear()
        self._busy = True
        self._fill_state = {
            "total": 0, "speed": 0.0, "elapsed": 0.0,
            "files": 0, "current": 0, "rotations": 0,
        }
        self.query_one("#scan", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self.query_one("#stop", Button).disabled = False

        self._log("─" * 60)
        self._log(f"[bold]目标：[/bold] {cfg.target}")
        self._log(f"[bold]文件系统：[/bold] {cfg.fs_type}")
        self._log(
            f"[bold]参数：[/bold] mode={cfg.mode}, "
            f"block={human_bytes(cfg.block_size)}, "
            f"reserve={human_bytes(cfg.reserve_bytes)}, "
            f"fsync_every={cfg.fsync_every}, "
            f"verify={cfg.verify}, keep={cfg.keep}, "
            f"max_file={human_bytes(cfg.max_file_size)}"
        )
        for w in fs_warnings(cfg.fs_type):
            self._log(w)
        if cfg.reserve_bytes == 0:
            self._log(
                "[yellow]注意：Reserve=0，将尝试写满整个文件系统。[/yellow]"
            )
        self._render_stats()
        self.run_fill(cfg)

    # ------------------------------------------------------ 扫描 worker

    @work(thread=True, exclusive=True, group="scan")
    def run_scan(self, target: str) -> None:
        start = time.monotonic()
        last_update = start
        file_count = 0
        total_size = 0
        errors = 0
        dirs_scanned = 0
        top_heap: list = []

        fs_type = detect_fs(target)
        self._safe_call(self._log, f"[bold]检测到文件系统：[/bold] {fs_type}")
        for w in fs_warnings(fs_type):
            self._safe_call(self._log, w)

        read_only = not os.access(target, os.W_OK | os.X_OK)
        if read_only:
            self._safe_call(self._log, "[red]目标目录不可写或不可进入。[/red]")

        stack = [target]
        try:
            while stack:
                if self._cancel_event.is_set():
                    break
                current = stack.pop()
                dirs_scanned += 1
                try:
                    with os.scandir(long_path(current)) as it:
                        for entry in it:
                            if self._cancel_event.is_set():
                                break
                            try:
                                if entry.is_symlink():
                                    continue
                                if entry.is_file(follow_symlinks=False):
                                    size = entry.stat(
                                        follow_symlinks=False
                                    ).st_size
                                    file_count += 1
                                    total_size += size
                                    if len(top_heap) < 10:
                                        heapq.heappush(
                                            top_heap, (size, entry.path)
                                        )
                                    elif size > top_heap[0][0]:
                                        heapq.heapreplace(
                                            top_heap, (size, entry.path)
                                        )
                                elif entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                            except OSError:
                                errors += 1
                except OSError:
                    errors += 1

                now = time.monotonic()
                if now - last_update >= 0.2:
                    self._safe_call(
                        self._on_scan_progress,
                        dirs_scanned, file_count, total_size,
                    )
                    last_update = now
        except Exception as e:  # noqa: BLE001
            self._safe_call(self._log, f"[red]扫描异常：{e!r}[/red]")

        elapsed = time.monotonic() - start
        try:
            du = shutil.disk_usage(target)
            disk_total, disk_used, disk_free = du.total, du.used, du.free
        except OSError as e:
            self._safe_call(self._log, f"[red]disk_usage 失败：{e}[/red]")
            disk_total = disk_used = disk_free = 0

        top_files = sorted(top_heap, key=lambda x: -x[0])
        result = ScanResult(
            target=target, fs_type=fs_type,
            file_count=file_count, total_size=total_size,
            errors=errors, top_files=top_files,
            disk_total=disk_total, disk_used=disk_used, disk_free=disk_free,
            elapsed=elapsed, read_only=read_only,
        )
        self._safe_call(self._on_scan_finished, result)

    # ------------------------------------------------------ 填充 worker

    @work(thread=True, exclusive=True, group="fill")
    def run_fill(self, cfg: FillConfig) -> None:
        target = cfg.target
        total_written = 0
        files_created: list = []
        start_time = time.monotonic()
        last_update = start_time
        stop_reason: Optional[str] = None
        file_index = 0
        file_bytes = 0
        rotations = 0
        max_file_size = cfg.max_file_size

        if cfg.mode == "random":
            random_block = os.urandom(cfg.block_size)
        else:
            random_block = None
        zero_block = b"\0" * cfg.block_size if cfg.mode == "zero" else None
        index_template = (
            bytearray(cfg.block_size) if cfg.mode == "index" else None
        )

        timestamp = time.strftime("%Y%m%d_%H%M%S")

        def emit_stats() -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 0.15:
                return
            elapsed = now - start_time
            speed = total_written / elapsed if elapsed > 0 else 0.0
            self._safe_call(
                self._on_fill_progress,
                total_written, speed, elapsed,
                len(files_created), file_bytes, rotations,
            )
            last_update = now

        try:
            while file_index < cfg.max_files:
                if self._cancel_event.is_set():
                    stop_reason = "用户取消"
                    break

                if cfg.reserve_bytes > 0:
                    try:
                        free = shutil.disk_usage(target).free
                    except OSError as e:
                        stop_reason = f"disk_usage 失败: {e}"
                        break
                    if free <= cfg.reserve_bytes:
                        stop_reason = (
                            f"达到预留空间 (free {human_bytes(free)} <= "
                            f"{human_bytes(cfg.reserve_bytes)})"
                        )
                        break

                path = os.path.join(
                    target,
                    f"{cfg.prefix}{timestamp}_{file_index:05d}.bin",
                )
                try:
                    fd = os.open(
                        long_path(path),
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except OSError as e:
                    if is_write_blocking_err(e):
                        stop_reason = f"无法创建文件：{e.strerror or e}"
                        break
                    raise

                files_created.append(path)
                file_bytes = 0
                block_no = 0
                file_stop = False
                rotated = False

                try:
                    while True:
                        if self._cancel_event.is_set():
                            stop_reason = "用户取消"
                            file_stop = True
                            break

                        if max_file_size and file_bytes >= max_file_size:
                            rotated = True
                            rotations += 1
                            break

                        if cfg.reserve_bytes > 0 and block_no % 64 == 0:
                            try:
                                free = shutil.disk_usage(target).free
                            except OSError:
                                free = None
                            if (free is not None
                                    and free <= cfg.reserve_bytes):
                                stop_reason = (
                                    f"达到预留空间 "
                                    f"(free {human_bytes(free)} <= "
                                    f"{human_bytes(cfg.reserve_bytes)})"
                                )
                                file_stop = True
                                break

                        if cfg.mode == "zero":
                            data = zero_block
                        elif cfg.mode == "index":
                            buf = bytearray(index_template)  # type: ignore
                            struct.pack_into("<Q", buf, 0, block_no)
                            data = bytes(buf)
                        else:
                            buf = bytearray(random_block)  # type: ignore
                            struct.pack_into("<Q", buf, 0, block_no)
                            data = bytes(buf)

                        view = memoryview(data)
                        while view:
                            try:
                                n = os.write(fd, view)
                            except OSError as e:
                                if is_write_blocking_err(e):
                                    stop_reason = (
                                        f"写入停止：{e.strerror or e}"
                                    )
                                    file_stop = True
                                    break
                                raise
                            if n == 0:
                                stop_reason = "磁盘已满 (write 返回 0)"
                                file_stop = True
                                break
                            view = view[n:]
                            total_written += n
                            file_bytes += n

                        if file_stop:
                            break
                        block_no += 1
                        if (cfg.fsync_every
                                and block_no % cfg.fsync_every == 0):
                            os.fsync(fd)
                        emit_stats()

                finally:
                    try:
                        os.fsync(fd)
                    except OSError:
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass

                if file_stop:
                    break

                if rotated:
                    self._safe_call(
                        self._log,
                        f"[dim]文件达到上限 "
                        f"{human_bytes(max_file_size)}，"
                        f"轮转新文件 (#{file_index + 1})[/dim]",
                    )
                    file_index += 1
                    continue

                file_index += 1

        except Exception as e:  # noqa: BLE001
            stop_reason = f"异常: {e!r}"
            self._safe_call(self._log, f"[red]{stop_reason}[/red]")

        elapsed = time.monotonic() - start_time
        speed = total_written / elapsed if elapsed > 0 else 0.0
        self._safe_call(
            self._on_fill_progress,
            total_written, speed, elapsed,
            len(files_created), file_bytes, rotations,
        )
        self._safe_call(
            self._log,
            f"[bold]写入结束[/bold] 总量 {human_bytes(total_written)} / "
            f"{fmt_duration(elapsed)} / 平均 {human_bytes(speed)}/s / "
            f"{len(files_created)} 个文件 / "
            f"原因：{stop_reason or '正常结束'}",
        )

        try:
            du = shutil.disk_usage(target)
            self._safe_call(
                self._log,
                f"[bold]磁盘现状[/bold] Used {human_bytes(du.used)} / "
                f"Total {human_bytes(du.total)} "
                f"({du.used / du.total * 100:.1f}%) | "
                f"Free {human_bytes(du.free)}",
            )
            if self._scan_result is not None:
                self._safe_call(
                    self._update_scan_disk_info,
                    du.total, du.used, du.free,
                )
        except OSError:
            pass

        if cfg.verify and files_created and not self._cancel_event.is_set():
            self._safe_call(self._log, "[cyan]开始读取校验…[/cyan]")
            try:
                errors, checked = self._verify_files(
                    cfg, files_created, random_block,
                )
                if errors == 0:
                    self._safe_call(
                        self._log,
                        f"[green]校验通过[/green]："
                        f"读取 {human_bytes(checked)}，无错误。",
                    )
                else:
                    self._safe_call(
                        self._log,
                        f"[red]校验发现 {errors} 处不匹配[/red]，"
                        f"读取 {human_bytes(checked)}。"
                        "可能是扩容盘/坏块/覆盖。",
                    )
            except Exception as e:  # noqa: BLE001
                self._safe_call(self._log, f"[red]校验异常：{e!r}[/red]")

        if not cfg.keep and files_created:
            self._safe_call(self._log, "清理测试文件…")
            for p in files_created:
                try:
                    os.unlink(long_path(p))
                except FileNotFoundError:
                    pass
                except OSError as e:
                    self._safe_call(
                        self._log, f"[red]删除失败 {p}: {e}[/red]"
                    )
            self._safe_call(self._log, "清理完成。")
        elif cfg.keep:
            self._safe_call(
                self._log, f"已保留 {len(files_created)} 个测试文件。"
            )

        self._safe_call(self._on_fill_finished)

    # ------------------------------------------------------ 校验

    def _verify_files(self, cfg, files_created, random_block):
        errors = 0
        checked = 0
        for path in files_created:
            if self._cancel_event.is_set():
                break
            try:
                with open(long_path(path), "rb") as f:
                    block_no = 0
                    while True:
                        data = f.read(cfg.block_size)
                        if not data:
                            break
                        if cfg.mode == "zero":
                            if data != b"\0" * len(data):
                                errors += 1
                        elif cfg.mode == "index":
                            if len(data) >= 8:
                                idx = struct.unpack("<Q", data[:8])[0]
                                if idx != block_no:
                                    errors += 1
                        else:
                            if len(data) >= 8:
                                idx = struct.unpack("<Q", data[:8])[0]
                                if idx != block_no:
                                    errors += 1
                                if random_block is not None:
                                    exp = bytearray(
                                        random_block[: len(data)]
                                    )
                                    struct.pack_into("<Q", exp, 0, block_no)
                                    if data != bytes(exp):
                                        errors += 1
                        checked += len(data)
                        block_no += 1
            except OSError:
                errors += 1
        return errors, checked

    # ------------------------------------------------------ UI 回调

    def _safe_call(self, fn, *args) -> None:
        try:
            self.call_from_thread(fn, *args)
        except Exception:  # noqa: BLE001
            pass

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _on_scan_progress(self, dirs: int, files: int, size: int) -> None:
        self._scan_progress = {"dirs": dirs, "files": files, "size": size}
        self._render_stats()

    def _on_scan_finished(self, result: ScanResult) -> None:
        self._scan_result = result
        self._busy = False
        self.query_one("#scan", Button).disabled = False
        self.query_one("#stop", Button).disabled = True

        self._log(
            f"[green]扫描完成[/green]：{result.file_count:,} 个文件，"
            f"{human_bytes(result.total_size)}，"
            f"{result.errors} 个错误，用时 {result.elapsed:.2f}s"
        )
        if result.top_files:
            self._log("[bold]目录中最大的文件：[/bold]")
            for size, path in result.top_files:
                self._log(f"  {human_bytes(size):>10}  {path}")

        self._render_stats()

        if self._pending_fill_cfg is not None:
            cfg = self._pending_fill_cfg
            self._pending_fill_cfg = None
            self._launch_fill(cfg)
        else:
            self.query_one("#start", Button).disabled = False

    def _update_scan_disk_info(self, total: int, used: int, free: int) -> None:
        if self._scan_result is not None:
            self._scan_result.disk_total = total
            self._scan_result.disk_used = used
            self._scan_result.disk_free = free
        self._render_stats()

    def _on_fill_progress(self, total, speed, elapsed, files, current,
                          rotations):
        self._fill_state = {
            "total": total, "speed": speed, "elapsed": elapsed,
            "files": files, "current": current, "rotations": rotations,
        }
        self._render_stats()

    def _on_fill_finished(self) -> None:
        self._busy = False
        self.query_one("#scan", Button).disabled = False
        self.query_one("#start", Button).disabled = False
        self.query_one("#stop", Button).disabled = True
        if self._scan_result is not None:
            try:
                du = shutil.disk_usage(self._scan_result.target)
                self._update_scan_disk_info(du.total, du.used, du.free)
            except OSError:
                pass
        self._render_stats()

    # ------------------------------------------------------ stats

    def _render_stats(self) -> None:
        lines: list = []
        scan = self._scan_result

        if self._busy and scan is None and self._scan_progress["dirs"] > 0:
            sp = self._scan_progress
            lines.append(
                f"[bold]Scanning…[/bold] dirs {sp['dirs']:,} | "
                f"files {sp['files']:,} | {human_bytes(sp['size'])}"
            )

        if scan is not None:
            total, used, free = (
                scan.disk_total, scan.disk_used, scan.disk_free
            )
            pct = (used / total * 100) if total else 0.0
            lines.append(f"[bold]FS:[/bold]       {scan.fs_type}")
            lines.append(
                f"[bold]Disk:[/bold]     Total {human_bytes(total)} | "
                f"Used {human_bytes(used)} ({pct:.1f}%) | "
                f"Free {human_bytes(free)}"
            )
            lines.append(
                f"[bold]Existing:[/bold] {scan.file_count:,} files, "
                f"{human_bytes(scan.total_size)} "
                "[dim](scanned target dir)[/dim]"
            )
        elif not (self._busy and self._scan_progress["dirs"] > 0):
            lines.append(
                "[bold]Disk:[/bold]     (未扫描，点 Scan 或直接 Start)"
            )

        st = self._fill_state
        if st["total"] > 0 or (self._busy and st["elapsed"] > 0):
            rot = (
                f" | Rotations {st['rotations']}" if st["rotations"] else ""
            )
            lines.append(
                f"[bold]This run:[/bold] Written "
                f"{human_bytes(st['total'])} | "
                f"{human_bytes(st['speed'])}/s | "
                f"{fmt_duration(st['elapsed'])} | "
                f"Files {st['files']}{rot}"
            )
            if scan is not None and scan.disk_total:
                est = scan.disk_used + st["total"]
                pct = est / scan.disk_total * 100
                lines.append(
                    f"[bold]Now used:[/bold] ~{human_bytes(est)} "
                    f"({pct:.1f}%) [dim][est.][/dim]"
                )

        if not lines:
            lines.append("Idle.")
        self.query_one("#stats-text", Static).update("\n".join(lines))