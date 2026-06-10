from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

from sub_pool_client import PooledCodexClient  # noqa: E402  -- imported after .env load

DATA_DIR = Path(os.environ.get("CODEX_QA_DATA_DIR", BASE_DIR / "instance"))
DB_PATH = DATA_DIR / "codex_qa.sqlite"
WORK_DIR = Path(os.environ.get("CODEX_QA_WORKDIR", DATA_DIR / "workspace"))
UPLOAD_DIR = WORK_DIR / "uploads"
RUN_DIR = DATA_DIR / "runs"

CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1200"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "high").strip() or "high"
CODEX_SERVICE_TIER = os.environ.get("CODEX_SERVICE_TIER", "fast").strip() or "fast"
MAX_CONTENT_LENGTH = int(os.environ.get("CODEX_QA_MAX_UPLOAD_MB", "80")) * 1024 * 1024
TEXT_EXCERPT_BYTES = int(os.environ.get("CODEX_QA_TEXT_EXCERPT_BYTES", "60000"))
WORKSPACE_TEXT_PREVIEW_BYTES = int(os.environ.get("CODEX_QA_WORKSPACE_TEXT_PREVIEW_BYTES", "524288"))
WORKSPACE_FILE_LIST_LIMIT = int(os.environ.get("CODEX_QA_WORKSPACE_FILE_LIST_LIMIT", "500"))
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}
TEXT_PREVIEW_EXTENSIONS = {
    ".csv",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".py",
    ".rst",
    ".text",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

DB_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def ensure_dirs() -> None:
    for path in (DATA_DIR, WORK_DIR, UPLOAD_DIR, RUN_DIR):
        path.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_connection() -> Iterable[sqlite3.Connection]:
    conn = connect_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    ensure_dirs()
    with DB_LOCK, db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                account_name TEXT,
                account_path TEXT,
                account_reason TEXT,
                codex_session_id TEXT,
                model TEXT,
                cwd TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                tools_count INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                workspace_refs_json TEXT NOT NULL DEFAULT '[]',
                note_md TEXT NOT NULL DEFAULT '',
                raw_events_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                bookmarked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(parent_id) REFERENCES nodes(id)
            );

            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                path TEXT NOT NULL,
                mime TEXT,
                size INTEGER NOT NULL,
                text_excerpt TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_usage (
                name TEXT PRIMARY KEY,
                used_tokens INTEGER NOT NULL DEFAULT 0,
                last_node_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_probe (
                name TEXT PRIMARY KEY,
                auth_file TEXT NOT NULL,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                answer TEXT
            );

            CREATE TABLE IF NOT EXISTS node_workspace_files (
                node_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(node_id, rel_path),
                FOREIGN KEY(node_id) REFERENCES nodes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_id ON nodes(parent_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
            CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at);
            CREATE INDEX IF NOT EXISTS idx_node_workspace_files_node_id ON node_workspace_files(node_id);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "workspace_refs_json" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN workspace_refs_json TEXT NOT NULL DEFAULT '[]'")
        if "note_md" not in columns:
            conn.execute("ALTER TABLE nodes ADD COLUMN note_md TEXT NOT NULL DEFAULT ''")


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def safe_json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def compact_title(text: str, limit: int = 44) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return "新问题"
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def public_node(row: sqlite3.Row) -> Dict[str, Any]:
    node = dict(row)
    node["attachments"] = safe_json_loads(node.pop("attachments_json", "[]"), [])
    node["workspace_refs"] = safe_json_loads(node.pop("workspace_refs_json", "[]"), [])
    node["raw_events"] = safe_json_loads(node.pop("raw_events_json", "[]"), [])
    for internal_key in ("account_name", "account_path", "account_reason"):
        node.pop(internal_key, None)
    node["archived"] = bool(node["archived"])
    node["bookmarked"] = bool(node["bookmarked"])
    return node


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return public_node(row) if row else None


def get_file(file_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def get_files(file_ids: Iterable[str]) -> List[Dict[str, Any]]:
    ids = [str(file_id) for file_id in file_ids if str(file_id).strip()]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM files WHERE id IN ({placeholders}) ORDER BY created_at",
            ids,
        ).fetchall()
    return rows_to_dicts(rows)


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def workspace_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = mimetypes.guess_type(path.name)[0] or ""
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix == ".pdf" or mime == "application/pdf":
        return "pdf"
    if mime.startswith("text/") or suffix in TEXT_PREVIEW_EXTENSIONS:
        return "text"
    return "file"


def public_workspace_file(path: Path, source_node: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    workspace_root = WORK_DIR.resolve()
    stat = path.stat()
    rel_path = path.resolve().relative_to(workspace_root).as_posix()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    item = {
        "path": rel_path,
        "absolute_path": str(path.resolve()),
        "name": path.name,
        "dir": str(Path(rel_path).parent) if str(Path(rel_path).parent) != "." else "",
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "mime": mime,
        "kind": workspace_kind(path),
    }
    if source_node:
        item["source_node_id"] = source_node.get("id")
        item["source_title"] = source_node.get("title") or "节点"
    return item


def public_workspace_ref(path: Path) -> Dict[str, Any]:
    item = public_workspace_file(path)
    return {
        "path": item["path"],
        "absolute_path": item["absolute_path"],
        "name": item["name"],
        "mime": item["mime"],
        "kind": item["kind"],
        "size": item["size"],
    }


def resolve_workspace_file(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("文件路径不能为空")

    workspace_root = WORK_DIR.resolve()
    upload_root = UPLOAD_DIR.resolve()
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / raw).resolve()

    if not path_is_within(path, workspace_root):
        raise ValueError("文件不在 workspace 内")
    if path_is_within(path, upload_root):
        raise ValueError("用户上传目录不在 Workspace 文件区中")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("文件不存在")
    return path


def normalize_workspace_refs(values: Iterable[Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen = set()
    for value in values:
        raw = value.get("path") if isinstance(value, dict) else value
        try:
            path = resolve_workspace_file(str(raw or ""))
        except (FileNotFoundError, ValueError):
            continue
        rel_path = path.resolve().relative_to(WORK_DIR.resolve()).as_posix()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        refs.append(public_workspace_ref(path))
    return refs


def workspace_snapshot() -> Dict[str, Tuple[Path, int, float]]:
    workspace_root = WORK_DIR.resolve()
    upload_root = UPLOAD_DIR.resolve()
    if not workspace_root.exists():
        return {}

    files: Dict[str, Tuple[Path, int, float]] = {}
    for path in workspace_root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if path_is_within(resolved, upload_root):
            continue
        try:
            stat = resolved.stat()
            rel_path = resolved.relative_to(workspace_root).as_posix()
        except OSError:
            continue
        files[rel_path] = (resolved, stat.st_size, stat.st_mtime)
    return files


def record_workspace_changes(node_id: str, before: Dict[str, Tuple[Path, int, float]]) -> None:
    after = workspace_snapshot()
    changed: List[Tuple[str, str, int, float, str]] = []
    for rel_path, (path, size, mtime) in after.items():
        previous = before.get(rel_path)
        if previous and previous[1] == size and abs(previous[2] - mtime) < 0.001:
            continue
        changed.append((node_id, rel_path, str(path), size, mtime, now_iso()))

    if not changed:
        return
    with DB_LOCK, db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO node_workspace_files (node_id, rel_path, path, size, mtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id, rel_path) DO UPDATE SET
                path = excluded.path,
                size = excluded.size,
                mtime = excluded.mtime,
                created_at = excluded.created_at
            """,
            changed,
        )


def inferred_workspace_files_for_nodes(nodes: List[Dict[str, Any]], known_paths: set[str]) -> List[Dict[str, Any]]:
    snapshot = workspace_snapshot()
    workspace_root = WORK_DIR.resolve()
    inferred: List[Dict[str, Any]] = []
    for rel_path, (path, _size, mtime) in snapshot.items():
        if rel_path in known_paths:
            continue
        absolute = str((workspace_root / rel_path).resolve())
        modified = datetime.fromtimestamp(mtime, timezone.utc)
        source_node = None
        for node in nodes:
            text = f"{node.get('prompt') or ''}\n{node.get('answer') or ''}\n{node.get('error') or ''}"
            if rel_path in text or absolute in text:
                source_node = node
            else:
                start = parse_iso(node.get("created_at"))
                end = parse_iso(node.get("completed_at") or node.get("updated_at"))
                if start <= modified <= end:
                    source_node = node
            if source_node:
                break
        if source_node:
            inferred.append(public_workspace_file(path, source_node))
            known_paths.add(rel_path)
    return inferred


def workspace_files_for_node(node_id: Optional[str]) -> List[Dict[str, Any]]:
    nodes = path_to_node(node_id)
    if not nodes:
        return []
    node_by_id = {node["id"]: node for node in nodes}
    node_ids = list(node_by_id.keys())
    placeholders = ",".join("?" for _ in node_ids)

    rows: List[sqlite3.Row] = []
    if placeholders:
        with DB_LOCK, db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM node_workspace_files
                WHERE node_id IN ({placeholders})
                ORDER BY mtime DESC
                """,
                node_ids,
            ).fetchall()

    known_paths: set[str] = set()
    items: List[Dict[str, Any]] = []
    for row in rows:
        rel_path = str(row["rel_path"])
        if rel_path in known_paths:
            continue
        known_paths.add(rel_path)
        try:
            path = resolve_workspace_file(rel_path)
        except (FileNotFoundError, ValueError):
            continue
        items.append(public_workspace_file(path, node_by_id.get(row["node_id"])))

    items.extend(inferred_workspace_files_for_nodes(nodes, known_paths))
    items.sort(key=lambda item: item.get("modified_at") or "", reverse=True)
    return items[:WORKSPACE_FILE_LIST_LIMIT]


def path_to_node(node_id: Optional[str]) -> List[Dict[str, Any]]:
    if not node_id:
        return []

    seen = set()
    path: List[Dict[str, Any]] = []
    current = node_id
    with DB_LOCK, db_connection() as conn:
        while current:
            if current in seen:
                break
            seen.add(current)
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (current,)).fetchone()
            if not row:
                break
            node = public_node(row)
            path.append(node)
            current = node.get("parent_id")
    path.reverse()
    return path


def account_usage_map() -> Dict[str, int]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute("SELECT name, used_tokens FROM account_usage").fetchall()
    return {row["name"]: int(row["used_tokens"]) for row in rows}


def make_attachment_context(files: List[Dict[str, Any]]) -> str:
    if not files:
        return "无"
    blocks = []
    for item in files:
        excerpt = (item.get("text_excerpt") or "").strip()
        block = [
            f"- 文件名: {item['original_name']}",
            f"  路径: {item['path']}",
            f"  MIME: {item.get('mime') or 'unknown'}",
            f"  大小: {item['size']} bytes",
        ]
        if excerpt:
            block.append("  摘要片段:")
            block.append(indent_block(excerpt[:12000], "    "))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def make_workspace_ref_context(refs: List[Dict[str, Any]]) -> str:
    if not refs:
        return "无"
    blocks = []
    for item in refs:
        blocks.append(
            "\n".join(
                [
                    f"- 文件名: {item.get('name') or Path(item.get('path') or '').name}",
                    f"  Workspace 相对路径: {item.get('path') or ''}",
                    f"  本地绝对路径: {item.get('absolute_path') or ''}",
                    f"  MIME: {item.get('mime') or 'unknown'}",
                    f"  类型: {item.get('kind') or 'file'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def inherited_attachment_files(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    file_ids: List[str] = []
    seen = set()
    for node in nodes:
        for attachment in node.get("attachments", []):
            file_id = str(attachment.get("id") or "").strip()
            if file_id and file_id not in seen:
                seen.add(file_id)
                file_ids.append(file_id)
    return get_files(file_ids)


def indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def build_codex_prompt(parent_id: Optional[str], prompt: str, files: List[Dict[str, Any]], workspace_refs: List[Dict[str, Any]]) -> str:
    inherited = path_to_node(parent_id)
    inherited_files = inherited_attachment_files(inherited)
    lines = [
        "你是 Codex 问答后端。当前应用把对话组织成树状结构；本次请求会从某个节点继承其祖先上下文，并产生一个新的子节点。",
        "请基于继承路径、用户当前问题和上传附件作答。若需要引用上传文件，优先使用给出的本地路径和片段。",
        "",
        "## 继承路径",
    ]
    if inherited:
        for index, node in enumerate(inherited, 1):
            lines.extend(
                [
                    f"### 节点 {index}: {node['title']}",
                    "User:",
                    node["prompt"].strip(),
                    "",
                    "Assistant:",
                    (node.get("answer") or "").strip() or "(该节点尚无完成回答)",
                    "",
                    "Workspace 引用:",
                    make_workspace_ref_context(node.get("workspace_refs") or []),
                    "",
                ]
            )
    else:
        lines.append("无，当前是根节点问题。")

    lines.extend(
        [
            "",
            "## 继承附件（祖先节点上传）",
            make_attachment_context(inherited_files),
            "",
            "## 本次上传附件",
            make_attachment_context(files),
            "",
            "## 本次 Workspace 引用",
            make_workspace_ref_context(workspace_refs),
            "",
            "## 当前用户问题",
            prompt.strip(),
        ]
    )
    return "\n".join(lines)


def create_node(parent_id: Optional[str], prompt: str, file_ids: List[str], workspace_refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if parent_id and not get_node(parent_id):
        raise ValueError("父节点不存在")

    files = get_files(file_ids)
    attachments = [
        {
            "id": item["id"],
            "name": item["original_name"],
            "path": item["path"],
            "mime": item.get("mime"),
            "size": item["size"],
        }
        for item in files
    ]

    node_id = uuid.uuid4().hex
    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO nodes (
                id, parent_id, title, prompt, status, attachments_json, workspace_refs_json, cwd,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                parent_id,
                compact_title(prompt),
                prompt,
                json.dumps(attachments, ensure_ascii=False),
                json.dumps(workspace_refs, ensure_ascii=False),
                str(WORK_DIR),
                created_at,
                created_at,
            ),
        )
    node = get_node(node_id)
    if not node:
        raise RuntimeError("节点创建失败")
    return node


def update_node(node_id: str, **fields: Any) -> None:
    allowed = {
        "title",
        "answer",
        "status",
        "account_name",
        "account_path",
        "account_reason",
        "codex_session_id",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "tools_count",
        "raw_events_json",
        "note_md",
        "error",
        "archived",
        "bookmarked",
        "completed_at",
        "updated_at",
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    payload["updated_at"] = payload.get("updated_at") or now_iso()
    if not payload:
        return
    assignments = ", ".join(f"{key} = ?" for key in payload.keys())
    values = list(payload.values()) + [node_id]
    with DB_LOCK, db_connection() as conn:
        conn.execute(f"UPDATE nodes SET {assignments} WHERE id = ?", values)


def nodes_for_project(root_id: str) -> List[Dict[str, Any]]:
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE project_nodes(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN project_nodes ON nodes.parent_id = project_nodes.id
            )
            SELECT nodes.* FROM nodes
            JOIN project_nodes ON nodes.id = project_nodes.id
            ORDER BY nodes.created_at
            """,
            (root_id,),
        ).fetchall()
    return [public_node(row) for row in rows]


def append_node_note(node_id: str, text: str) -> Dict[str, Any]:
    addition = text.strip()
    if not addition:
        raise ValueError("追加内容不能为空")
    with DB_LOCK, db_connection() as conn:
        row = conn.execute("SELECT note_md FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            raise ValueError("节点不存在")
        current = str(row["note_md"] or "").rstrip()
        next_note = f"{current}\n\n{addition}" if current else addition
        conn.execute(
            "UPDATE nodes SET note_md = ?, updated_at = ? WHERE id = ?",
            (next_note, now_iso(), node_id),
        )
    node = get_node(node_id)
    if not node:
        raise ValueError("节点不存在")
    return node


def add_account_usage(account_name: str, node_id: str, tokens: int) -> None:
    if not account_name or tokens <= 0:
        return
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO account_usage (name, used_tokens, last_node_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                used_tokens = used_tokens + excluded.used_tokens,
                last_node_id = excluded.last_node_id,
                updated_at = excluded.updated_at
            """,
            (account_name, int(tokens), node_id, now_iso()),
        )


def collect_usage(events: List[Dict[str, Any]], prompt_text: str, answer: str) -> Dict[str, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    def visit(obj: Any) -> None:
        nonlocal prompt_tokens, completion_tokens, total_tokens
        if isinstance(obj, dict):
            lowered = {str(k).lower(): v for k, v in obj.items()}
            for key in ("prompt_tokens", "input_tokens"):
                value = lowered.get(key)
                if isinstance(value, (int, float)):
                    prompt_tokens = max(prompt_tokens, int(value))
            for key in ("completion_tokens", "output_tokens"):
                value = lowered.get(key)
                if isinstance(value, (int, float)):
                    completion_tokens = max(completion_tokens, int(value))
            value = lowered.get("total_tokens")
            if isinstance(value, (int, float)):
                total_tokens = max(total_tokens, int(value))
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    visit(events)
    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    if not total_tokens:
        total_tokens = max(1, int((len(prompt_text) + len(answer)) / 3.5))
        prompt_tokens = max(1, int(len(prompt_text) / 3.5))
        completion_tokens = max(1, total_tokens - prompt_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def collect_tools_count(events: List[Dict[str, Any]]) -> int:
    tool_item_ids = set()
    for event in events:
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type in {"command_execution", "web_search"}:
                tool_item_ids.add(str(item.get("id") or len(tool_item_ids)))
                continue
        text = json.dumps(event, ensure_ascii=False).lower()
        if "tool_call" in text or "tool_use" in text or "function_call" in text:
            tool_item_ids.add(str(event.get("id") or len(tool_item_ids)))
    return len(tool_item_ids)


def find_first_key(events: List[Dict[str, Any]], keys: Tuple[str, ...]) -> Optional[str]:
    lowered_keys = {key.lower() for key in keys}

    def visit(obj: Any) -> Optional[str]:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in lowered_keys and isinstance(value, str) and value:
                    return value
            for value in obj.values():
                found = visit(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = visit(value)
                if found:
                    return found
        return None

    return visit(events)


def extract_answer_from_events(events: List[Dict[str, Any]]) -> str:
    candidates: List[str] = []
    preferred_types = (
        "agent_message",
        "assistant_message",
        "final_answer",
        "response.completed",
        "message",
    )

    for event in events:
        event_type = str(event.get("type") or event.get("event") or "").lower()
        if event_type and not any(kind in event_type for kind in preferred_types):
            continue
        for key in ("message", "content", "text", "answer", "final_answer"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        item = event.get("item")
        if isinstance(item, dict):
            for key in ("message", "content", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
    return candidates[-1] if candidates else ""


def parse_jsonl_events(stdout_text: str) -> List[Dict[str, Any]]:
    events = []
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def parse_jsonl_line(line: str) -> Optional[Dict[str, Any]]:
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


async def _run_codex_for_node_async(node_id: str) -> None:
    node = get_node(node_id)
    if not node:
        return

    files = get_files([item["id"] for item in node.get("attachments", [])])
    prompt_text = build_codex_prompt(node.get("parent_id"), node["prompt"], files, node.get("workspace_refs") or [])
    run_path = RUN_DIR / node_id
    run_path.mkdir(parents=True, exist_ok=True)
    final_message_path = run_path / "final.md"
    before_workspace = workspace_snapshot()

    stdout_lines: List[str] = []
    events: List[Dict[str, Any]] = []
    stderr_text = ""
    returncode: Optional[int] = None
    timed_out = False
    account_name: Optional[str] = None
    lease_id: Optional[str] = None
    rate_limited = False

    try:
        async with PooledCodexClient(required_model=CODEX_MODEL or None) as client:
            account_name = client.account
            lease_id = client.lease_id
            update_node(
                node_id,
                status="running",
                account_name=account_name,
                account_path=f"sub-pool lease {lease_id or ''}".strip(),
                account_reason=f"sub-pool 自动分配（model={CODEX_MODEL}）",
                model=CODEX_MODEL or None,
            )

            cmd = [
                "codex",
                "--search",
                "-c",
                f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
                "-c",
                f'service_tier="{CODEX_SERVICE_TIER}"',
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(WORK_DIR),
                "-o",
                str(final_message_path),
                "-m",
                CODEX_MODEL,
                "-",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(BASE_DIR),
                env=client.env,
            )

            async def pump_stdout() -> None:
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    stdout_lines.append(line)
                    event = parse_jsonl_line(line)
                    if not event:
                        continue
                    events.append(event)
                    public_events = trim_events_for_storage(events)
                    update_node(
                        node_id,
                        raw_events_json=json.dumps(public_events, ensure_ascii=False),
                        tools_count=collect_tools_count(events),
                        codex_session_id=find_first_key(events, ("thread_id", "session_id", "sessionId", "conversation_id")),
                    )

            try:
                if proc.stdin is not None:
                    proc.stdin.write(prompt_text.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                await asyncio.wait_for(pump_stdout(), timeout=CODEX_TIMEOUT_SECONDS)
                returncode = await asyncio.wait_for(proc.wait(), timeout=30)
                stderr_bytes = await proc.stderr.read() if proc.stderr else b""
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    returncode = await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    returncode = -1
                if proc.stderr:
                    try:
                        stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                    except asyncio.TimeoutError:
                        pass

            stdout_text = "".join(stdout_lines)
            combined = f"{stderr_text}\n{stdout_text}".lower()
            if returncode and returncode != 0 and ("429" in combined or "rate limit" in combined or "rate_limit" in combined):
                rate_limited = True
                try:
                    await client.report_error("429", (stderr_text or stdout_text)[-500:])
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        update_node(node_id, status="failed", error=f"sub-pool 调用失败：{exc}", completed_at=now_iso())
        return

    stdout_text = "".join(stdout_lines)
    answer = ""
    if final_message_path.exists():
        answer = final_message_path.read_text(encoding="utf-8", errors="replace").strip()
    if not answer:
        answer = extract_answer_from_events(events).strip()

    record_workspace_changes(node_id, before_workspace)
    usage = collect_usage(events, prompt_text, answer or stderr_text)
    session_id = find_first_key(events, ("session_id", "sessionId", "conversation_id"))
    tools_count = collect_tools_count(events)
    public_events = trim_events_for_storage(events)

    if timed_out:
        update_node(
            node_id,
            status="failed",
            error=f"Codex 超时：超过 {CODEX_TIMEOUT_SECONDS} 秒未完成",
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
        )
    elif returncode == 0 and answer:
        update_node(
            node_id,
            status="done",
            answer=answer,
            codex_session_id=session_id,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            tools_count=tools_count,
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            completed_at=now_iso(),
            error=None,
        )
        if account_name:
            add_account_usage(account_name, node_id, usage["total_tokens"])
    else:
        error = stderr_text.strip() or stdout_text.strip() or "Codex 执行失败"
        if rate_limited:
            error = f"[已上报 sub-pool 冷却该账号] {error}"
        update_node(
            node_id,
            status="failed",
            answer=answer,
            raw_events_json=json.dumps(public_events, ensure_ascii=False),
            error=error[-8000:],
            completed_at=now_iso(),
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
        )
        if account_name:
            add_account_usage(account_name, node_id, usage["total_tokens"])


def run_codex_for_node(node_id: str) -> None:
    try:
        asyncio.run(_run_codex_for_node_async(node_id))
    except Exception as exc:  # noqa: BLE001
        update_node(node_id, status="failed", error=str(exc), completed_at=now_iso())


def trim_events_for_storage(events: List[Dict[str, Any]], max_events: int = 80) -> List[Dict[str, Any]]:
    trimmed = events[-max_events:]
    serialized = json.dumps(trimmed, ensure_ascii=False)
    if len(serialized) <= 120000:
        return trimmed
    return [{"type": "truncated", "message": "事件过大，已截断存储", "events": len(events)}]


def start_worker(node_id: str) -> None:
    thread = threading.Thread(target=run_codex_for_node, args=(node_id,), daemon=True)
    thread.start()


def is_text_like(filename: str, mime: Optional[str]) -> bool:
    if mime and (mime.startswith("text/") or mime in {"application/json", "application/xml"}):
        return True
    return Path(filename).suffix.lower() in {
        ".txt",
        ".md",
        ".rst",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".csv",
        ".log",
        ".html",
        ".css",
        ".xml",
        ".tex",
    }


def store_upload(file_storage: Any) -> Dict[str, Any]:
    original_name = file_storage.filename or "upload"
    safe_name = secure_filename(original_name) or "upload"
    file_id = uuid.uuid4().hex
    target_dir = UPLOAD_DIR / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    file_storage.save(target_path)

    mime = mimetypes.guess_type(original_name)[0] or file_storage.mimetype
    size = target_path.stat().st_size
    text_excerpt = ""
    if is_text_like(original_name, mime):
        text_excerpt = target_path.read_bytes()[:TEXT_EXCERPT_BYTES].decode(
            "utf-8", errors="replace"
        )

    created_at = now_iso()
    with DB_LOCK, db_connection() as conn:
        conn.execute(
            """
            INSERT INTO files (id, original_name, stored_name, path, mime, size, text_excerpt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                original_name,
                safe_name,
                str(target_path),
                mime,
                size,
                text_excerpt,
                created_at,
            ),
        )
    saved = get_file(file_id)
    if not saved:
        raise RuntimeError("文件保存失败")
    return saved


def export_subtree_markdown(root_id: str) -> str:
    root = get_node(root_id)
    if not root:
        raise ValueError("节点不存在")
    with DB_LOCK, db_connection() as conn:
        rows = conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
    nodes = [public_node(row) for row in rows]
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for node in nodes:
        children.setdefault(node.get("parent_id"), []).append(node)

    lines = [f"# {root['title']}", ""]

    def walk(node: Dict[str, Any], depth: int) -> None:
        heading = "#" * min(depth + 2, 6)
        lines.extend(
            [
                f"{heading} {node['title']}",
                "",
                f"- 状态: {node['status']}",
                f"- Token: {node.get('total_tokens') or 0}",
                "",
                "**User**",
                "",
                node["prompt"].strip(),
                "",
                "**Assistant**",
                "",
                (node.get("answer") or "").strip() or "(无回答)",
                "",
            ]
        )
        for child in children.get(node["id"], []):
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


@app.get("/api/health")
def health() -> Response:
    return jsonify({"ok": True, "time": now_iso(), "auth_selection": "fixed_backend_auth"})


@app.get("/api/tree")
def api_tree() -> Response:
    with DB_LOCK, db_connection() as conn:
        node_rows = conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
        file_rows = conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return jsonify(
        {
            "nodes": [public_node(row) for row in node_rows],
            "files": rows_to_dicts(file_rows),
            "work_dir": str(WORK_DIR),
        }
    )


@app.get("/api/accounts")
def api_accounts() -> Response:
    return jsonify({"managed_by_backend": True, "selection": "fixed"})


CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)


def claude_code_slug(project_path: str) -> str:
    return re.sub(r"[/_]", "-", str(project_path).rstrip("/"))


def _user_text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def _is_claude_user_prompt(msg: Dict[str, Any]) -> bool:
    if msg.get("type") != "user":
        return False
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
    return bool(_user_text_blocks(content))


def _count_tool_uses(msg: Dict[str, Any]) -> int:
    content = (msg.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")


def _assistant_usage(msg: Dict[str, Any]) -> Tuple[int, int]:
    usage = (msg.get("message") or {}).get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    prompt += int(usage.get("cache_creation_input_tokens") or 0)
    prompt += int(usage.get("cache_read_input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return prompt, completion


def read_claude_code_nodes(cc_dir: Path) -> List[Dict[str, Any]]:
    raw: Dict[str, Dict[str, Any]] = {}
    # For each session file, capture the LAST `last-prompt` event's leafUuid.
    # That is the message --resume <session_id> will land on.
    session_resume_leaf: Dict[str, str] = {}
    for jsonl_path in sorted(cc_dir.glob("*.jsonl")):
        session_id = jsonl_path.stem
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "last-prompt" and msg.get("leafUuid"):
                        session_resume_leaf[session_id] = msg["leafUuid"]
                        continue
                    uuid_ = msg.get("uuid")
                    if uuid_ and uuid_ not in raw:
                        raw[uuid_] = msg
        except OSError:
            continue

    def find_user_parent(uuid_: str) -> Optional[str]:
        seen = set()
        msg = raw.get(uuid_)
        if not msg:
            return None
        parent = msg.get("parentUuid")
        while parent and parent not in seen:
            seen.add(parent)
            p = raw.get(parent)
            if not p:
                return None
            if _is_claude_user_prompt(p):
                return parent
            parent = p.get("parentUuid")
        return None

    user_uuids = [u for u, m in raw.items() if _is_claude_user_prompt(m)]

    # session_id -> user_prompt uuid that --resume lands on (walk up from leafUuid until user prompt)
    resume_user_by_session: Dict[str, str] = {}
    # inverse: user_prompt uuid -> list of session_ids whose resume lands here
    sessions_resuming_to: Dict[str, List[str]] = {}
    for sid, leaf_uuid in session_resume_leaf.items():
        cursor = leaf_uuid
        seen: set = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            m = raw.get(cursor)
            if not m:
                break
            if _is_claude_user_prompt(m):
                resume_user_by_session[sid] = cursor
                sessions_resuming_to.setdefault(cursor, []).append(sid)
                break
            cursor = m.get("parentUuid")

    # children map among user prompts (for BFS to find branch-end resume target)
    user_children: Dict[Optional[str], List[str]] = {}
    for u in user_uuids:
        parent = find_user_parent(u)
        user_children.setdefault(parent, []).append(u)

    # For each user node, find the nearest descendant (BFS) that is an exact resume target.
    # If found, that descendant's session is the "branch-end resume" for this node.
    branch_end_session: Dict[str, str] = {}
    branch_end_node: Dict[str, str] = {}
    for u in user_uuids:
        if u in sessions_resuming_to:
            continue
        queue = list(user_children.get(u, []))
        visited = {u, *queue}
        while queue:
            cur = queue.pop(0)
            if cur in sessions_resuming_to:
                branch_end_session[u] = sessions_resuming_to[cur][-1]
                branch_end_node[u] = cur
                break
            for child in user_children.get(cur, []):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)

    descendants_of: Dict[str, List[Dict[str, Any]]] = {u: [] for u in user_uuids}

    for uuid_, msg in raw.items():
        if _is_claude_user_prompt(msg):
            continue
        owner = find_user_parent(uuid_)
        if owner and owner in descendants_of:
            descendants_of[owner].append(msg)

    nodes: List[Dict[str, Any]] = []
    for uuid_ in user_uuids:
        user_msg = raw[uuid_]
        parent_user = find_user_parent(uuid_)
        prompt_text = _user_text_blocks((user_msg.get("message") or {}).get("content"))

        descendants = descendants_of.get(uuid_, [])
        descendants.sort(key=lambda m: m.get("timestamp") or "")

        answer_parts: List[str] = []
        tools_count = 0
        prompt_tokens = 0
        completion_tokens = 0
        model = ""
        for d in descendants:
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            answer_parts.append(text)
            elif isinstance(content, str) and content.strip():
                answer_parts.append(content.strip())
            tools_count += _count_tool_uses(d)
            pt, ct = _assistant_usage(d)
            prompt_tokens += pt
            completion_tokens += ct
            if not model and msg.get("model"):
                model = str(msg.get("model"))

        answer = "\n\n".join(answer_parts)
        last_ts = (descendants[-1].get("timestamp") if descendants else user_msg.get("timestamp")) or ""

        # With --resume-session-at we can always resume to this exact message.
        # The session arg is the file this user prompt lives in; the message arg is its uuid.
        own_session = user_msg.get("sessionId") or ""
        resume_sessions = sessions_resuming_to.get(uuid_, [])
        # Keep these informational fields for the UI to optionally surface.
        if resume_sessions:
            resume_kind = "exact"
            resume_descendant = ""
        elif uuid_ in branch_end_session:
            resume_kind = "branch_end"
            resume_descendant = branch_end_node[uuid_]
        else:
            resume_kind = "fallback"
            resume_descendant = ""
        primary_resume = own_session

        nodes.append({
            "id": f"cc:{uuid_}",
            "parent_id": f"cc:{parent_user}" if parent_user else None,
            "title": compact_title(prompt_text or "（空消息）"),
            "prompt": prompt_text,
            "answer": answer,
            "status": "done",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "tools_count": tools_count,
            "attachments": [],
            "workspace_refs": [],
            "raw_events": [],
            "note_md": "",
            "model": model,
            "cwd": user_msg.get("cwd") or "",
            "created_at": user_msg.get("timestamp") or "",
            "completed_at": last_ts,
            "updated_at": last_ts,
            "archived": False,
            "bookmarked": False,
            "codex_session_id": primary_resume,
            "resume_sessions": resume_sessions,
            "resume_kind": resume_kind,
            "resume_descendant_id": f"cc:{resume_descendant}" if resume_descendant else "",
            "own_session_id": own_session,
            "resume_message_uuid": uuid_,
            "source": "claude_code",
        })

    return nodes


@app.get("/api/claude-code/tree")
def api_claude_code_tree() -> Response:
    project_path = (request.args.get("path") or "").strip()
    if not project_path:
        return jsonify({"error": "path 不能为空"}), 400
    slug = claude_code_slug(project_path)
    cc_dir = CLAUDE_PROJECTS_DIR / slug
    if not cc_dir.exists() or not cc_dir.is_dir():
        return jsonify({
            "error": f"未找到 Claude Code 记录目录：{cc_dir}",
            "slug": slug,
        }), 404
    try:
        nodes = read_claude_code_nodes(cc_dir)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"读取失败：{exc}"}), 500
    return jsonify({
        "nodes": nodes,
        "source_path": project_path,
        "claude_dir": str(cc_dir),
    })


@app.post("/api/ask")
def api_ask() -> Response:
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt 不能为空"}), 400
    parent_id = payload.get("parent_id") or None
    file_ids = payload.get("file_ids") or []
    if not isinstance(file_ids, list):
        return jsonify({"error": "file_ids 必须是数组"}), 400
    workspace_refs_payload = payload.get("workspace_refs") or []
    if not isinstance(workspace_refs_payload, list):
        return jsonify({"error": "workspace_refs 必须是数组"}), 400
    workspace_refs = normalize_workspace_refs(workspace_refs_payload)
    try:
        node = create_node(parent_id, prompt, file_ids, workspace_refs)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    start_worker(node["id"])
    return jsonify({"node": node}), 202


@app.post("/api/upload")
def api_upload() -> Response:
    uploaded = request.files.getlist("files")
    if not uploaded:
        uploaded = request.files.getlist("file")
    if not uploaded:
        return jsonify({"error": "没有收到文件"}), 400
    saved_files = [store_upload(item) for item in uploaded if item.filename]
    return jsonify({"files": saved_files}), 201


@app.get("/api/files/<file_id>/download")
def api_download_file(file_id: str) -> Any:
    item = get_file(file_id)
    if not item:
        return jsonify({"error": "文件不存在"}), 404
    return send_file(item["path"], as_attachment=True, download_name=item["original_name"])


@app.get("/api/workspace/files")
def api_workspace_files() -> Response:
    node_id = str(request.args.get("node_id") or "").strip() or None
    return jsonify({"root": str(WORK_DIR), "files": workspace_files_for_node(node_id)})


@app.get("/api/workspace/content")
def api_workspace_content() -> Response:
    try:
        path = resolve_workspace_file(str(request.args.get("path") or ""))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    kind = workspace_kind(path)
    if kind not in {"markdown", "text"}:
        return jsonify({"error": "该文件类型不支持文本预览"}), 415

    with path.open("rb") as file_obj:
        data = file_obj.read(WORKSPACE_TEXT_PREVIEW_BYTES + 1)
    truncated = len(data) > WORKSPACE_TEXT_PREVIEW_BYTES
    if truncated:
        data = data[:WORKSPACE_TEXT_PREVIEW_BYTES]
    text = data.decode("utf-8", errors="replace")
    return jsonify({"file": public_workspace_file(path), "content": text, "truncated": truncated})


@app.get("/api/workspace/file")
def api_workspace_file() -> Any:
    try:
        path = resolve_workspace_file(str(request.args.get("path") or ""))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    download = str(request.args.get("download") or "") == "1"
    return send_file(
        path,
        as_attachment=download,
        download_name=path.name,
        mimetype=mimetypes.guess_type(path.name)[0] or None,
        conditional=True,
    )


@app.patch("/api/nodes/<node_id>/note")
def api_patch_node_note(node_id: str) -> Response:
    if not get_node(node_id):
        return jsonify({"error": "节点不存在"}), 404
    payload = request.get_json(silent=True) or {}
    note_md = payload.get("note_md", "")
    if not isinstance(note_md, str):
        note_md = str(note_md)
    update_node(node_id, note_md=note_md)
    return jsonify({"node": get_node(node_id)})


@app.post("/api/nodes/<node_id>/note/append")
def api_append_node_note(node_id: str) -> Response:
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    if not isinstance(text, str):
        text = str(text)
    try:
        node = append_node_note(node_id, text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"node": node})


@app.get("/api/projects/<project_id>/notes")
def api_project_notes(project_id: str) -> Response:
    project = get_node(project_id)
    if not project:
        return jsonify({"error": "项目不存在"}), 404
    notes = [
        {
            "id": node["id"],
            "title": node.get("title") or "节点",
            "note_md": node.get("note_md") or "",
            "created_at": node.get("created_at"),
            "updated_at": node.get("updated_at"),
        }
        for node in nodes_for_project(project_id)
        if str(node.get("note_md") or "").strip()
    ]
    return jsonify({"project": {"id": project["id"], "title": project.get("title") or "项目"}, "notes": notes})


@app.patch("/api/nodes/<node_id>")
def api_patch_node(node_id: str) -> Response:
    if not get_node(node_id):
        return jsonify({"error": "节点不存在"}), 404
    payload = request.get_json(silent=True) or {}
    updates: Dict[str, Any] = {}
    if "title" in payload:
        title = compact_title(str(payload["title"]), 80)
        if title:
            updates["title"] = title
    if "archived" in payload:
        updates["archived"] = 1 if bool(payload["archived"]) else 0
    if "bookmarked" in payload:
        updates["bookmarked"] = 1 if bool(payload["bookmarked"]) else 0
    update_node(node_id, **updates)
    return jsonify({"node": get_node(node_id)})


@app.delete("/api/nodes/<node_id>")
def api_delete_node(node_id: str) -> Response:
    node = get_node(node_id)
    if not node:
        return jsonify({"error": "节点不存在"}), 404
    with DB_LOCK, db_connection() as conn:
        count_row = conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            SELECT COUNT(*) AS count FROM doomed
            """,
            (node_id,),
        ).fetchone()
        deleted_count = int(count_row["count"] if count_row else 0)
        conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            DELETE FROM node_workspace_files WHERE node_id IN (SELECT id FROM doomed)
            """,
            (node_id,),
        )
        conn.execute(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN doomed ON nodes.parent_id = doomed.id
            )
            DELETE FROM nodes WHERE id IN (SELECT id FROM doomed)
            """,
            (node_id,),
        )
    return jsonify({"deleted": deleted_count, "parent_id": node.get("parent_id")})


@app.get("/api/export/<node_id>")
def api_export(node_id: str) -> Any:
    try:
        markdown = export_subtree_markdown(node_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    filename = f"codex-tree-{node_id[:8]}.md"
    return Response(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


init_db()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)
