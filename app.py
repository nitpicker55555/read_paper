from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CODEX_QA_DATA_DIR", BASE_DIR / "instance"))
DB_PATH = DATA_DIR / "codex_qa.sqlite"
WORK_DIR = Path(os.environ.get("CODEX_QA_WORKDIR", DATA_DIR / "workspace"))
UPLOAD_DIR = WORK_DIR / "uploads"
RUN_DIR = DATA_DIR / "runs"
CODEX_HOME_DIR = DATA_DIR / "codex_homes"
CODEX_AUTH_FILE = Path(
    os.environ.get(
        "CODEX_AUTH_FILE",
        "/Users/puzhen/PycharmProjects/datagen_mcp_toolathlon_ver_2/codex_token/auth_outlook_puzhen.json",
    )
)
USER_CONFIG = Path(os.environ.get("CODEX_USER_CONFIG", Path.home() / ".codex" / "config.toml"))

CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1200"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "").strip()
MAX_CONTENT_LENGTH = int(os.environ.get("CODEX_QA_MAX_UPLOAD_MB", "80")) * 1024 * 1024
TEXT_EXCERPT_BYTES = int(os.environ.get("CODEX_QA_TEXT_EXCERPT_BYTES", "60000"))


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
    for path in (DATA_DIR, WORK_DIR, UPLOAD_DIR, RUN_DIR, CODEX_HOME_DIR):
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

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_id ON nodes(parent_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
            CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at);
            """
        )


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


def select_account() -> Dict[str, Any]:
    if not CODEX_AUTH_FILE.exists():
        raise RuntimeError(f"指定的 Codex auth 文件不存在：{CODEX_AUTH_FILE}")
    return {
        "name": CODEX_AUTH_FILE.stem,
        "auth_path": str(CODEX_AUTH_FILE),
        "auth_file": CODEX_AUTH_FILE.name,
        "reason": "固定使用用户指定的 Codex auth 文件",
    }


def prepare_codex_home(account: Dict[str, Any]) -> Path:
    safe_name = secure_filename(account["name"]) or "account"
    home = CODEX_HOME_DIR / safe_name
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(account["auth_path"], home / "auth.json")
    try:
        os.chmod(home / "auth.json", 0o600)
    except OSError:
        pass
    if USER_CONFIG.exists():
        shutil.copy2(USER_CONFIG, home / "config.toml")
    return home


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


def build_codex_prompt(parent_id: Optional[str], prompt: str, files: List[Dict[str, Any]]) -> str:
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
            "## 当前用户问题",
            prompt.strip(),
        ]
    )
    return "\n".join(lines)


def create_node(parent_id: Optional[str], prompt: str, file_ids: List[str]) -> Dict[str, Any]:
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
                id, parent_id, title, prompt, status, attachments_json, cwd,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                node_id,
                parent_id,
                compact_title(prompt),
                prompt,
                json.dumps(attachments, ensure_ascii=False),
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


def run_codex_for_node(node_id: str) -> None:
    node = get_node(node_id)
    if not node:
        return

    try:
        account = select_account()
        files = get_files([item["id"] for item in node.get("attachments", [])])
        prompt_text = build_codex_prompt(node.get("parent_id"), node["prompt"], files)
        codex_home = prepare_codex_home(account)
        run_path = RUN_DIR / node_id
        run_path.mkdir(parents=True, exist_ok=True)
        final_message_path = run_path / "final.md"

        update_node(
            node_id,
            status="running",
            account_name=account["name"],
            account_path=account["auth_file"],
            account_reason=account["reason"],
            model=CODEX_MODEL or None,
        )

        cmd = [
            "codex",
            "--search",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(WORK_DIR),
            "-o",
            str(final_message_path),
        ]
        if CODEX_MODEL:
            cmd.extend(["-m", CODEX_MODEL])
        cmd.append("-")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(BASE_DIR),
            env=env,
        )

        timed_out = {"value": False}

        def kill_on_timeout() -> None:
            timed_out["value"] = True
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(CODEX_TIMEOUT_SECONDS, kill_on_timeout)
        timer.start()
        stdout_lines: List[str] = []
        events: List[Dict[str, Any]] = []
        try:
            if proc.stdin:
                proc.stdin.write(prompt_text)
                proc.stdin.close()

            if proc.stdout:
                for line in proc.stdout:
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
            returncode = proc.wait()
            stderr_text = proc.stderr.read() if proc.stderr else ""
        finally:
            timer.cancel()

        stdout_text = "".join(stdout_lines)
        answer = ""
        if final_message_path.exists():
            answer = final_message_path.read_text(encoding="utf-8", errors="replace").strip()
        if not answer:
            answer = extract_answer_from_events(events).strip()

        usage = collect_usage(events, prompt_text, answer or stderr_text)
        session_id = find_first_key(events, ("session_id", "sessionId", "conversation_id"))
        tools_count = collect_tools_count(events)
        public_events = trim_events_for_storage(events)

        if timed_out["value"]:
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
            add_account_usage(account["name"], node_id, usage["total_tokens"])
        else:
            error = stderr_text.strip() or stdout_text.strip() or "Codex 执行失败"
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
            add_account_usage(account["name"], node_id, usage["total_tokens"])
    except subprocess.TimeoutExpired:
        update_node(
            node_id,
            status="failed",
            error=f"Codex 超时：超过 {CODEX_TIMEOUT_SECONDS} 秒未完成",
            completed_at=now_iso(),
        )
    except Exception as exc:  # noqa: BLE001 - surface background worker failures in UI
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
    try:
        node = create_node(parent_id, prompt, file_ids)
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
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)
