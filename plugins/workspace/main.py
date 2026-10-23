"""Workspace 插件：只读访问用户在配置中指定的目录（root）。

宿主没有沙箱，路径限制由插件自己实现：所有路径解析（包括跟随符号链接）后必须位于 root 之内；
二进制文件和超过 max_file_bytes 的文件不读取、不搜索；隐藏文件和目录（以 . 开头）跳过。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from harness_sdk import Context, Plugin, ToolError, ToolOutput

plugin = Plugin()

DEFAULT_MAX_FILE_BYTES = 1024 * 1024
BINARY_SNIFF_BYTES = 8192
MAX_LINE_CHARS = 500


def root_of(ctx: Context) -> Path:
    raw = ctx.config.get("root")
    if not raw:
        raise ToolError("没有配置 root，请先执行 harness plugin config workspace --set root=<目录>")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ToolError(f"root 必须是绝对路径：{raw}")
    root = root.resolve()
    if not root.is_dir():
        raise ToolError(f"root 目录不存在：{root}")
    return root


def resolve(ctx: Context, relative: str) -> tuple[Path, Path]:
    """把相对路径解析到 root 之内；拒绝 ..、绝对路径和指向 root 之外的符号链接。"""
    root = root_of(ctx)
    target = (root / relative).resolve()
    if target != root and not target.is_relative_to(root):
        raise ToolError(f"路径 {relative} 位于允许访问的目录之外，已拒绝")
    if not target.exists():
        raise ToolError(f"路径不存在：{relative}")
    return root, target


def display_path(root: Path, path: Path) -> str:
    return "." if path == root else path.relative_to(root).as_posix()


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def readable_text(path: Path, max_bytes: int) -> str | None:
    """返回不能读取的原因；可以读取时返回 None。"""
    size = path.stat().st_size
    if size > max_bytes:
        return f"文件大小 {size} 字节，超过上限 {max_bytes}"
    with path.open("rb") as file:
        if b"\0" in file.read(BINARY_SNIFF_BYTES):
            return "是二进制文件"
    return None


def walk_files(root: Path, start: Path, recursive: bool) -> Iterator[Path]:
    """按名称排序遍历，跳过隐藏项和指向 root 之外的符号链接；不跟随目录链接。"""
    if start.is_file():
        yield start
        return
    for current, dirs, files in os.walk(start):
        dirs[:] = sorted(d for d in dirs if not is_hidden(d)) if recursive else []
        for name in sorted(files):
            if is_hidden(name):
                continue
            path = Path(current) / name
            resolved = path.resolve()
            if resolved.is_relative_to(root) and resolved.is_file():
                yield path


def page(total: int, offset: int, items: list[Any], key: str) -> dict[str, Any]:
    """分页约定：返回 total 和 next_offset，没有更多时 next_offset 为 null。"""
    end = offset + len(items)
    return {
        "total": total,
        "offset": offset,
        "next_offset": end if end < total else None,
        key: items,
    }


@plugin.tool("list_files")
def list_files(
    ctx: Context, path: str = ".", recursive: bool = False, limit: int = 100, offset: int = 0
) -> ToolOutput:
    root, target = resolve(ctx, path)
    if not target.is_dir():
        raise ToolError(f"{path} 不是目录")
    entries: list[dict[str, Any]] = []
    if not recursive:
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if is_hidden(child.name):
                continue
            resolved = child.resolve()
            if not resolved.is_relative_to(root):
                continue
            kind = "dir" if resolved.is_dir() else "file"
            entry: dict[str, Any] = {"path": display_path(root, child), "type": kind}
            if kind == "file":
                entry["size"] = resolved.stat().st_size
            entries.append(entry)
    else:
        entries = [
            {"path": display_path(root, f), "type": "file", "size": f.stat().st_size}
            for f in walk_files(root, target, recursive=True)
        ]
    items = entries[offset : offset + limit]
    lines = [f"- {e['path']}{'/' if e['type'] == 'dir' else ''}" for e in items]
    return ToolOutput(
        content={
            "path": display_path(root, target),
            **page(len(entries), offset, items, "entries"),
        },
        display=f"{display_path(root, target)} 下共 {len(entries)} 项\n\n" + "\n".join(lines),
    )


@plugin.tool("read_file")
def read_file(ctx: Context, path: str, start_line: int = 1, max_lines: int = 200) -> ToolOutput:
    root, target = resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"{path} 不是文件")
    max_bytes = int(ctx.config.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    problem = readable_text(target, max_bytes)
    if problem:
        raise ToolError(f"无法读取 {path}：{problem}")
    all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(all_lines)
    if total and start_line > total:
        raise ToolError(f"start_line={start_line} 超出范围，文件共 {total} 行")
    selected = all_lines[start_line - 1 : start_line - 1 + max_lines]
    end_line = start_line + len(selected) - 1
    return ToolOutput(
        content={
            "path": display_path(root, target),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total,
            "next_start_line": end_line + 1 if end_line < total else None,
            "text": "\n".join(selected),
        },
        display=f"已读取 {display_path(root, target)} 第 {start_line}–{end_line} 行（共 {total} 行）",
    )


@plugin.tool("search_text")
def search_text(
    ctx: Context, query: str, path: str = ".", limit: int = 20, offset: int = 0
) -> ToolOutput:
    if not query.strip():
        raise ToolError("搜索关键词不能为空")
    root, target = resolve(ctx, path)
    max_bytes = int(ctx.config.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    skipped = 0
    for file in walk_files(root, target, recursive=True):
        if readable_text(file, max_bytes):
            skipped += 1
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line.casefold():
                matches.append(
                    {
                        "path": display_path(root, file),
                        "line": number,
                        "text": line[:MAX_LINE_CHARS],
                    }
                )
    items = matches[offset : offset + limit]
    summary = f"找到 {len(matches)} 处包含「{query}」的内容"
    if skipped:
        summary += f"（跳过 {skipped} 个二进制或过大的文件）"
    lines = [f"- {m['path']}:{m['line']} {m['text'].strip()}" for m in items]
    return ToolOutput(
        content={
            "query": query,
            "skipped_files": skipped,
            **page(len(matches), offset, items, "matches"),
        },
        display=summary + ("\n\n" + "\n".join(lines) if lines else ""),
    )


if __name__ == "__main__":
    plugin.run()
