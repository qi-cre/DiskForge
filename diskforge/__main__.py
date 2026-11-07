"""命令行入口。"""

from __future__ import annotations

import argparse
import sys

from . import __app_name__, __version__
from .app import DiskForgeApp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diskforge",
        description=(
            f"{__app_name__} — 磁盘填充 / 写入速度 / 真实容量测试工具。"
            "危险操作，仅用于专用测试盘。"
        ),
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"{__app_name__} {__version__}",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="可选：启动时预填目标目录",
    )
    args = parser.parse_args(argv)

    app = DiskForgeApp()
    if args.target:
        # 通过 post-mount 回调预填输入框
        def _prefill() -> None:
            try:
                app.query_one("#target").value = args.target
            except Exception:
                pass
        app.call_after_refresh(_prefill)

    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())