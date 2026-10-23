"""Notes 插件：把笔记保存在插件数据目录下的 SQLite 数据库中。

每次工具调用都是一个新进程；写操作在 BEGIN IMMEDIATE 事务中完成，
多个进程同时写入时由 SQLite 的锁串行化，不会互相覆盖。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import Any

from harness_sdk import Context, Plugin, ToolError, ToolOutput

plugin = Plugin()

DEFAULT_MAX_NOTES = 1000
SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""


def connect(ctx: Context) -> sqlite3.Connection:
    db = sqlite3.connect(ctx.data_dir / "notes.db", timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute(SCHEMA)
    return db


@contextmanager
def write_transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "title", "body", "created_at")}


def to_markdown(notes: list[dict[str, Any]]) -> str:
    return "\n".join(f"- **#{n['id']} {n['title']}**：{n['body']}" for n in notes)


@plugin.tool("add_note")
def add_note(ctx: Context, title: str, body: str) -> ToolOutput:
    title = title.strip()
    if not title:
        raise ToolError("标题不能为空")
    max_notes = int(ctx.config.get("max_notes", DEFAULT_MAX_NOTES))
    with closing(connect(ctx)) as db, write_transaction(db):
        (count,) = db.execute("SELECT COUNT(*) FROM notes").fetchone()
        if count >= max_notes:
            raise ToolError(f"笔记数量已达上限 {max_notes}，请先删除旧笔记或调大 max_notes 配置")
        cursor = db.execute("INSERT INTO notes (title, body) VALUES (?, ?)", (title, body))
    note_id = cursor.lastrowid
    return ToolOutput(
        content={"id": note_id, "title": title}, display=f"已保存笔记 #{note_id}「{title}」"
    )


@plugin.tool("list_notes")
def list_notes(ctx: Context, limit: int = 20, offset: int = 0) -> ToolOutput:
    with closing(connect(ctx)) as db:
        (total,) = db.execute("SELECT COUNT(*) FROM notes").fetchone()
        rows = db.execute(
            "SELECT * FROM notes ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    notes = [to_dict(row) for row in rows]
    display = to_markdown(notes) if notes else "还没有笔记。"
    return ToolOutput(
        content=page(total, offset, notes),
        display=f"共 {total} 条笔记\n\n{display}",
    )


@plugin.tool("search_notes")
def search_notes(ctx: Context, query: str, limit: int = 10, offset: int = 0) -> ToolOutput:
    query = query.strip()
    if not query:
        raise ToolError("搜索关键词不能为空")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    condition = "title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'"
    with closing(connect(ctx)) as db:
        (total,) = db.execute(
            f"SELECT COUNT(*) FROM notes WHERE {condition}", (pattern, pattern)
        ).fetchone()
        rows = db.execute(
            f"SELECT * FROM notes WHERE {condition} ORDER BY id DESC LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
        ).fetchall()
    notes = [to_dict(row) for row in rows]
    if not total:
        display = f"没有找到包含「{query}」的笔记。"
    else:
        display = f"找到 {total} 条包含「{query}」的笔记\n\n{to_markdown(notes)}"
    return ToolOutput(
        content={"query": query, **page(total, offset, notes)},
        display=display,
    )


def page(total: int, offset: int, notes: list[dict[str, Any]]) -> dict[str, Any]:
    """分页约定（DESIGN.md §8.4）：返回 total 和 next_offset，没有更多时 next_offset 为 null。"""
    end = offset + len(notes)
    return {
        "total": total,
        "offset": offset,
        "next_offset": end if end < total else None,
        "notes": notes,
    }


if __name__ == "__main__":
    plugin.run()
