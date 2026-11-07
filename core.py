"""填充/扫描的核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .fs import DEFAULT_MAX_FILE_SIZE


@dataclass
class ScanResult:
    target: str
    fs_type: str = "unknown"
    file_count: int = 0
    total_size: int = 0
    errors: int = 0
    top_files: list = field(default_factory=list)   # [(size, path), ...]
    disk_total: int = 0
    disk_used: int = 0
    disk_free: int = 0
    elapsed: float = 0.0
    read_only: bool = False


@dataclass
class FillConfig:
    target: str
    block_size: int
    reserve_bytes: int
    max_files: int
    mode: str          # zero | index | random
    fsync_every: int
    verify: bool
    keep: bool
    fs_type: str = "unknown"
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    prefix: str = "diskforge_test_"