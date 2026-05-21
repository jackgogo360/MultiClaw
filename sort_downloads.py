#!/usr/bin/env python3
"""对下载目录下的所有文件按大小排序，输出前10个最大的文件。"""

import os
from pathlib import Path

# ---------- 配置 ----------
DOWNLOAD_DIR = Path.home() / "Downloads"   # 下载目录，可按需修改
TOP_N = 10                                 # 输出前 N 个
MIN_SIZE = 0                               # 最小文件大小（字节），0 表示不过滤
# -------------------------


def human_size(size_bytes: int) -> str:
    """将字节数转为可读格式（KB/MB/GB）。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def collect_files(directory: Path) -> list[tuple[Path, int]]:
    """递归收集目录下所有文件及其大小，返回 [(路径, 大小), ...]。"""
    files = []
    try:
        for entry in directory.rglob("*"):
            if entry.is_file(follow_symlinks=False):
                size = entry.stat().st_size
                if size >= MIN_SIZE:
                    files.append((entry, size))
    except PermissionError:
        print(f"⚠️  无权限访问: {directory}")
    return files


def main():
    if not DOWNLOAD_DIR.exists():
        print(f"❌ 目录不存在: {DOWNLOAD_DIR}")
        return

    print(f"📂 正在扫描: {DOWNLOAD_DIR}")
    files = collect_files(DOWNLOAD_DIR)

    if not files:
        print("📭 未找到任何文件。")
        return

    # 按大小降序排列
    files.sort(key=lambda x: x[1], reverse=True)

    total = len(files)
    print(f"📊 共找到 {total} 个文件，以下是前 {min(TOP_N, total)} 个：\n")

    header = f"{'排名':<6} {'文件大小':<14} {'路径'}"
    print(header)
    print("-" * len(header))

    for rank, (path, size) in enumerate(files[:TOP_N], start=1):
        # 尝试用相对路径显示，更简洁
        try:
            display = path.relative_to(DOWNLOAD_DIR)
        except ValueError:
            display = path
        print(f"{rank:<6} {human_size(size):<14} {display}")


if __name__ == "__main__":
    main()
