"""自定义控件：磁盘问题确认对话框。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .health import DiskIssue


class DiskCheckScreen(ModalScreen):
    """显示磁盘问题，让用户选择运行检查或继续。"""

    CSS = """
    DiskCheckScreen { align: center middle; }
    #dialog {
        width: 90; max-width: 95%; height: auto; max-height: 85%;
        border: thick $warning; background: $surface;
        padding: 1 2;
    }
    #dialog-title { text-style: bold; padding-bottom: 1; }
    #dialog-issues {
        height: auto; max-height: 60%;
        overflow-y: auto; padding: 1 0;
    }
    .issue { padding: 0 0 1 0; }
    #dialog-actions {
        height: auto; align: center middle; padding-top: 1;
    }
    #dialog-actions Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, issues: list[DiskIssue], target: str) -> None:
        super().__init__()
        self._issues = issues
        self._target = target

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(
                f"[bold]DiskForge 检测到磁盘问题[/bold]：{self._target}",
                id="dialog-title",
            )
            with Container(id="dialog-issues"):
                for issue in self._issues:
                    color = {
                        "error": "red",
                        "warning": "yellow",
                        "info": "cyan",
                    }.get(issue.level, "white")
                    yield Static(
                        f"[{color}]{issue.level.upper():7}[/{color}] "
                        f"[dim]({issue.source})[/dim] {issue.message}",
                        markup=True, classes="issue",
                    )
                    if issue.can_check:
                        yield Static(
                            f"    [dim]可用检查：{issue.check_label}[/dim]",
                            markup=True, classes="issue",
                        )
            yield Static(
                "\n是否在继续前运行磁盘检查？"
                "（检查均为只读，不会修改数据）"
            )
            with Horizontal(id="dialog-actions"):
                yield Button("运行检查并继续", id="run", variant="warning")
                yield Button("跳过并继续", id="skip", variant="primary")
                yield Button("取消", id="cancel", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "run":
            self.dismiss("run")
        elif bid == "skip":
            self.dismiss("skip")
        else:
            self.dismiss("cancel")

    def action_cancel(self) -> None:
        self.dismiss("cancel")