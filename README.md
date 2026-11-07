# 🛠️ DiskForge

> **DiskForge** 是一个基于 [Textual](https://github.com/Textualize/textual) 构建的现代化终端（TUI）磁盘填充测试工具。
> 用于测试存储设备（U 盘、SD 卡、移动硬盘）的**真实写入速度**、**真实容量**，并安全地**检测扩容盘**。

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/TUI-Textual-purple.svg)](https://github.com/Textualize/textual)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows-lightgrey.svg)]()
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

> *贡献代码即代表同意以 GPL-3.0 协议发布。*

## ✨ 核心特性

- **📊 现代化 TUI 界面**：无需记忆复杂的命令行参数，直观的仪表盘实时显示写入速度、耗时、进度和文件数量。
- **🛡️ 安全优先设计**：通过文件系统写入测试文件，**绝不直接写入裸设备（如 `/dev/sdX`）**，避免误毁分区表。
- **🔍 真实容量验证**：支持 `random` 和 `index` 模式，写满后自动读回校验，准确识别无良商家的“扩容盘”。
- **💾 多文件系统适配**：自动识别 `exFAT`、`NTFS`、`FAT32`、`ext4` 等。遇到 FAT32 单文件 4GiB 限制时，自动轮转新文件。
- **🩺 磁盘健康预检**：读写前自动检查挂载状态（只读/错误策略）、SMART 健康状态（Linux）、卷健康（Windows），并在发现问题时弹窗询问是否运行只读检查。
- **🚫 设备过滤**：支持通过 udev 规则自动忽略特定设备（如 PlutoSDR 等模拟 U 盘的设备）。
- **🪟 跨平台支持**：完美支持 Linux 和 Windows（内置 Windows 长路径 `\\?\` 处理）。

---

## 📦 安装指南

请确保你的电脑已安装 [Python 3.9+](https://www.python.org/downloads/)。

### Linux / macOS

建议使用 `virtualenv` 创建隔离环境（Arch Linux 用户推荐）：

    # 克隆仓库
    git clone https://github.com/qi-cre/DiskForge.git
    cd DiskForge

    # 创建并激活虚拟环境
    python3 -m venv .venv
    source .venv/bin/activate

    # 安装（使用国内镜像源加速）
    pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple

### Windows

打开 PowerShell（建议以管理员身份运行，以便预检读取卷信息）：

    # 克隆仓库
    git clone https://github.com/qi-cre/DiskForge.git
    cd DiskForge

    # 创建并激活虚拟环境
    python -m venv .venv-win
    .\.venv-win\Scripts\Activate.ps1

    # 安装（使用国内镜像源加速）
    pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple

---

## 🚀 使用说明

在终端中输入以下命令启动 TUI 界面：

    dforge
    # 或者
    diskforge

启动后，按 `Tab` 键切换焦点，填写目标目录（例如 Linux 下的 `/mnt/usb`，Windows 下的 `E:\`），然后按 `s` 开始测试。

### 快捷键

| 按键 | 功能 |
| :--- | :--- |
| `s` | 开始（Start） |
| `x` | 停止（Stop / 中断测试） |
| `r` | 重新扫描目标目录（Scan） |
| `q` / `Ctrl+C` | 退出程序 |

### 核心参数解析

| 参数 | 推荐设置 | 说明 |
| :--- | :--- | :--- |
| **Mode** | `zero` / `index` | `zero` 最快，只测纯写入速度；`index` 每块写入块号，用于配合校验检测覆盖；`random` 最严格，生成随机数据。 |
| **Reserve (MiB)** | 测容量填 `0` | 预留空间。日常测速建议留 `64`；测扩容盘需填 `0` 写满全盘。 |
| **fsync every** | 测纯速度填 `0` | 每写多少块强制刷盘。填 `8` 较真实（能看到 U 盘物理极限）；填 `0` 会利用系统缓存（速度极快但不真实）。 |
| **Verify** | 测扩容盘勾选 | 写满后自动读回校验，如果报大量不匹配，说明是扩容盘。 |
| **Keep files** | 可选 | 测试结束不删除测试文件，方便拔出后重新插入再手动验证。 |

---

## ⚠️ 安全警告 (非常重要)

1. **请勿在系统盘运行**：即使有 `Reserve` 保护，写满系统分区依然可能导致系统崩溃或程序报错。
2. **专用盘测试**：请确保测试盘（U 盘/移动硬盘）内**没有重要数据**。虽然 DiskForge 只是创建测试文件，但不排除极端情况下文件系统崩溃。
3. **拔盘前请安全卸载**：测试结束后，请务必执行 `sync && umount /mnt/usb`（Linux）或在 Windows 任务栏点击“安全弹出”，切勿直接硬拔。

---

## 🔧 进阶技巧

### 1. 为什么写入速度和日常拷贝不一样？
日常拷贝文件时，操作系统会将数据先存入内存（Page Cache），此时看到的几十/几百 MB/s 是内存的速度。DiskForge 开启了 `fsync`，能测出闪存颗粒**真实的掉速后稳态速度**。
如果想测纯峰值，在界面中将 `fsync every` 设为 `0` 即可。

### 2. Linux 下过滤特殊设备（如 PlutoSDR）
如果你的电脑连接了模拟成 U 盘的设备（如 PlutoSDR），可以添加 udev 规则让 `udisks2` 忽略它：

    sudo tee /etc/udev/rules.d/99-plutosdr-ignore.rules <<'EOF'
    ACTION=="add", SUBSYSTEM=="block", ENV{ID_VENDOR_ID}=="0456", ENV{UDISKS_IGNORE}="1"
    EOF

### 3. 遇到 `NotAuthorized` 无法挂载？
Linux 下的普通用户挂载依赖 `polkit`。如果提示无权限，请直接使用 `sudo mount -o uid=$(id -u),gid=$(id -g) /dev/sdX /mnt/usb` 即可。

---

## 👨‍💻 开发与致谢

- UI 驱动：[Textual](https://github.com/Textualize/textual)
- 灵感来源：`h2testw`, `F3`

如果这个工具帮你揪出了一个虚标 U 盘或扩容盘，或者帮你测出了设备的真实速度，欢迎给个 ⭐️ Star！

## 🤝 贡献指南

欢迎提交 Issue 或 Pull Request！
贡献代码即代表同意以 [GPL-3.0](LICENSE) 协议发布。