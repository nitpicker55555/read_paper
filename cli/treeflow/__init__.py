"""treeflow — browse and resume any node in your local Claude Code or Codex
CLI conversation tree, including abandoned sibling branches that stock
`--resume` can't reach natively.

Quick usage:
    treeflow                       interactive list of conversations in current dir
    treeflow -L                    only branch tails (leaf nodes)
    treeflow search "browser"      keyword search across prompts
    treeflow resume <uuid-prefix>  generate resume command for a specific node
    treeflow -x                    after picking, exec the agent CLI for you
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # 3.11+ stdlib
except ImportError:  # pragma: no cover  -- 3.10 fallback
    tomllib = None  # type: ignore[assignment]


# ---------- config ----------

_CONFIG_DEFAULTS: Dict[str, Any] = {
    "page_size": 15,
    "tool": "claude",                    # which agent CLI to target by default
    "paths": {
        "claude_projects_dir": "~/.claude/projects",
        "codex_sessions_dir": "~/.codex/sessions",
    },
    "browser": {
        "default_view": "tree",
        "tree_order": "oldest_first",
        # When true, picking a node in the interactive browser immediately
        # execs the agent CLI on the resume target instead of just printing
        # the command. Same as passing `-x` / `--exec` at the top level.
        "auto_exec": False,
    },
    "debug": {"log_keys": False},
}


def _load_config() -> Dict[str, Any]:
    """Layer config: in-code defaults → bundled treeflow.toml → user override.

    User override paths (first that exists wins, after defaults+bundled):
      $TREEFLOW_CONFIG    — explicit path, if set
      ~/.config/treeflow/treeflow.toml
      ~/.treeflow.toml

    Missing keys at any layer fall through to the previous layer, so old
    user configs keep working after we add fields."""
    candidates: List[Path] = [Path(__file__).parent / "treeflow.toml"]
    env_override = os.environ.get("TREEFLOW_CONFIG")
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.append(Path.home() / ".config" / "treeflow" / "treeflow.toml")
    candidates.append(Path.home() / ".treeflow.toml")

    merged: Dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v)
                              for k, v in _CONFIG_DEFAULTS.items()}
    if tomllib is None:
        return merged
    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except Exception:  # noqa: BLE001  -- malformed config never crashes treeflow
            continue
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
    return merged


_CONFIG = _load_config()


def _cfg(*keys: str, default: Any = None) -> Any:
    """Walk into _CONFIG by section + key. _cfg('browser', 'tree_order')."""
    cur: Any = _CONFIG
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vwidth(s: str) -> int:
    """Display cells for `s` after stripping ANSI escapes. Counts East Asian
    Wide/Fullwidth chars (CJK, fullwidth punctuation) as 2 cells."""
    plain = _ANSI_RE.sub("", s)
    n = 0
    for c in plain:
        n += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return n


def _truncate_w(s: str, max_w: int, ellipsis: str = "…") -> str:
    """Truncate raw text (no ANSI inside) so visual width ≤ max_w."""
    if max_w <= 0:
        return ""
    n = 0
    out = []
    cap = max(1, max_w - _vwidth(ellipsis))
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if n + cw > cap:
            return "".join(out) + ellipsis
        out.append(c)
        n += cw
    return "".join(out)


CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR")
    or os.path.expanduser(_cfg("paths", "claude_projects_dir",
                               default="~/.claude/projects"))
).expanduser()


# Sentinel returned by pickers when the user wants to back up one layer.
# `None` keeps meaning "quit the whole interactive flow".
class _BackSentinel:
    def __repr__(self) -> str: return "BACK"


BACK = _BackSentinel()


# ---------- ansi colors ----------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _USE_COLOR else text


def dim(t: str) -> str: return _c(t, "2")
def bold(t: str) -> str: return _c(t, "1")
def purple(t: str) -> str: return _c(t, "35")
def green(t: str) -> str: return _c(t, "32")
def cyan(t: str) -> str: return _c(t, "36")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str: return _c(t, "31")


# ---------- path / slug ----------

def slug_for(path: str) -> str:
    return re.sub(r"[/_]", "-", str(path).rstrip("/"))


def project_dir_for(path: str) -> Path:
    return CLAUDE_PROJECTS_DIR / slug_for(path)


# ---------- jsonl parsing ----------

def _user_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = str(b.get("text") or "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _is_user_prompt(msg: dict) -> bool:
    if msg.get("type") != "user":
        return False
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return False
    return bool(_user_text(content))


def load_project(project_dir: Path) -> Tuple[Dict[str, Tuple[dict, str]], Dict[str, str]]:
    """Read every jsonl in project_dir.

    Returns (by_uuid, session_leaf):
      by_uuid[uuid] = (parsed_msg, raw_jsonl_line)
      session_leaf[sid] = latest leafUuid recorded in <sid>.jsonl's last-prompt events
    """
    by_uuid: Dict[str, Tuple[dict, str]] = {}
    session_leaf: Dict[str, str] = {}
    for jsonl_path in sorted(project_dir.glob("*.jsonl")):
        sid = jsonl_path.stem
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    stripped = line.strip()
                    if not stripped.startswith("{"):
                        continue
                    try:
                        d = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "last-prompt" and d.get("leafUuid"):
                        session_leaf[sid] = d["leafUuid"]
                        continue
                    u = d.get("uuid")
                    if u and u not in by_uuid:
                        by_uuid[u] = (d, line)
        except OSError:
            continue
    return by_uuid, session_leaf


def build_nodes(
    by_uuid: Dict[str, Tuple[dict, str]],
    session_leaf: Dict[str, str],
):
    """Return (nodes_by_uuid, native_reachable_set)."""
    user_uuids = [u for u, (m, _) in by_uuid.items() if _is_user_prompt(m)]

    def find_user_parent(uid: str) -> Optional[str]:
        cur = by_uuid.get(uid)
        if not cur:
            return None
        parent = cur[0].get("parentUuid")
        seen: set = set()
        while parent and parent not in seen:
            seen.add(parent)
            p = by_uuid.get(parent)
            if not p:
                return None
            if _is_user_prompt(p[0]):
                return parent
            parent = p[0].get("parentUuid")
        return None

    nodes: Dict[str, dict] = {}
    for u in user_uuids:
        m, _ = by_uuid[u]
        nodes[u] = {
            "uuid": u,
            "parent": find_user_parent(u),
            "children": [],
            "text": _user_text((m.get("message") or {}).get("content")),
            "timestamp": m.get("timestamp") or "",
            "session_id": m.get("sessionId") or "",
            "cwd": m.get("cwd") or "",
            # session id whose stock --resume natively lands here (None if not native)
            "native_session_id": None,
            # assistant reply text whose nearest user-prompt ancestor is this node;
            # populated in the answer-aggregation pass below. Used by the browser's
            # "search prompts + replies" mode.
            "answer_text": "",
        }
    for u, n in nodes.items():
        if n["parent"] and n["parent"] in nodes:
            nodes[n["parent"]]["children"].append(u)

    # Aggregate assistant text per user node so the browser can search replies
    # too. For each non-user msg, walk up parentUuid until we hit a user prompt
    # ancestor — that node "owns" the reply. Done in one pass over by_uuid.
    answer_buf: Dict[str, List[str]] = {u: [] for u in user_uuids}
    for uid, (msg, _line) in by_uuid.items():
        if _is_user_prompt(msg):
            continue
        if msg.get("type") != "assistant":
            continue
        owner = find_user_parent(uid)
        if not owner or owner not in answer_buf:
            continue
        content = (msg.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            answer_buf[owner].append(content.strip())
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = str(block.get("text") or "").strip()
                    if t:
                        answer_buf[owner].append(t)
    for u, parts in answer_buf.items():
        if parts:
            nodes[u]["answer_text"] = "\n".join(parts)

    # native_reachable: set of user-prompt uuids that --resume <sid> physically lands on
    native_reachable: set = set()
    for sid, leaf in session_leaf.items():
        cur = leaf
        seen: set = set()
        while cur and cur not in seen:
            seen.add(cur)
            m = by_uuid.get(cur)
            if not m:
                break
            if _is_user_prompt(m[0]):
                native_reachable.add(cur)
                if cur in nodes and nodes[cur]["native_session_id"] is None:
                    nodes[cur]["native_session_id"] = sid
                break
            cur = m[0].get("parentUuid")

    return nodes, native_reachable


# ---------- resume (synthetic jsonl) ----------

def write_synthetic_session(project_dir: Path, target_uuid: str,
                            by_uuid: Dict[str, Tuple[dict, str]]) -> Tuple[str, int, Path]:
    """Walk parentUuid from target back to root, write a fresh <new-sid>.jsonl pinning
    the active leaf at target. Returns (new_sid, chain_length, file_path)."""
    chain: List[str] = []
    cur: Optional[str] = target_uuid
    seen: set = set()
    while cur and cur not in seen:
        seen.add(cur)
        entry = by_uuid.get(cur)
        if not entry:
            break
        chain.append(entry[1])
        cur = entry[0].get("parentUuid")
    chain.reverse()

    new_sid = str(uuid.uuid4())
    new_path = project_dir / f"{new_sid}.jsonl"

    target_msg, _ = by_uuid[target_uuid]
    raw_content = (target_msg.get("message") or {}).get("content")
    target_text = ""
    if isinstance(raw_content, str):
        target_text = raw_content
    elif isinstance(raw_content, list):
        for b in raw_content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = str(b.get("text") or "").strip()
                if t:
                    target_text = t
                    break

    last_prompt = json.dumps({
        "type": "last-prompt",
        "lastPrompt": target_text,
        "leafUuid": target_uuid,
        "sessionId": new_sid,
    }, ensure_ascii=False)

    with new_path.open("w", encoding="utf-8") as f:
        for line in chain:
            f.write(line + "\n")
        f.write(last_prompt + "\n")

    return new_sid, len(chain), new_path


def emit_resume(project_dir: Path, node: dict, by_uuid: Dict[str, Tuple[dict, str]],
                exec_after: bool) -> None:
    # Native shortcut: when this node is the leafUuid resolution of some session
    # that already exists on disk, stock `claude --resume <that-session>` lands
    # exactly here. No need to write a synthetic file.
    native_sid = node.get("native_session_id") if isinstance(node, dict) else None
    if native_sid:
        cmd_str = f"claude --resume {native_sid}"
        print()
        print(green(f"  ✓ native resume target — using original session, no file written"))
        print(bold(f"  $ {cmd_str}"))
        print()
        if exec_after:
            print(dim("  → exec…"))
            try:
                os.execvp("claude", ["claude", "--resume", native_sid])
            except FileNotFoundError:
                print(red("  ! `claude` not found — install Claude Code CLI and put it on PATH"))
                sys.exit(2)
        return

    new_sid, chain_len, new_path = write_synthetic_session(project_dir, node["uuid"], by_uuid)
    cmd_str = f"claude --resume {new_sid}"
    print()
    print(green(f"  ✓ synthesized session ({chain_len}-link chain) → {new_path.name}"))
    print(bold(f"  $ {cmd_str}"))
    print()
    if exec_after:
        print(dim("  → exec…"))
        try:
            os.execvp("claude", ["claude", "--resume", new_sid])
        except FileNotFoundError:
            print(red("  ! `claude` not found — install Claude Code CLI and put it on PATH"))
            sys.exit(2)


# ---------- codex (~/.codex/sessions/) ----------
#
# Codex stores each conversation as one jsonl rollout file under
# `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl`. The file is a
# strictly linear event log (no parentUuid threading). Branches across sessions
# are recorded via `session_meta.payload.forked_from_id`. Codex has no native
# way to land a `--resume` mid-session — we synthesize a truncated rollout
# file pinned to a new session id, validated empirically.

CODEX_SESSIONS_DIR = Path(
    os.environ.get("CODEX_SESSIONS_DIR")
    or os.path.expanduser(_cfg("paths", "codex_sessions_dir",
                               default="~/.codex/sessions"))
).expanduser()


def _codex_session_files() -> List[Path]:
    if not CODEX_SESSIONS_DIR.exists():
        return []
    return sorted(CODEX_SESSIONS_DIR.rglob("*.jsonl"))


def _codex_read_session_meta(jsonl_path: Path) -> Optional[dict]:
    """Read the first line of a Codex rollout — `session_meta`. Returns the
    payload dict (with `id`, `cwd`, `forked_from_id`, `timestamp`, …) or None
    if the file is malformed."""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
        d = json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("type") != "session_meta":
        return None
    return d.get("payload") or {}


def _codex_is_real_user_text(text: str) -> bool:
    """Codex injects `<environment_context>` and `<turn_aborted>` blocks as
    role=user messages. Filter them out — we only want messages the human typed."""
    if not text:
        return False
    t = text.strip()
    if t.startswith("<environment_context>"):
        return False
    if t.startswith("<turn_aborted>"):
        return False
    return True


def _codex_extract_text(payload: dict) -> str:
    """Concatenate text from a response_item.payload's content blocks."""
    parts: List[str] = []
    for c in (payload.get("content") or []):
        if not isinstance(c, dict):
            continue
        t = c.get("text") or c.get("input_text") or c.get("output_text") or ""
        if t:
            parts.append(t)
    return "\n".join(parts).strip()


def _codex_parse_session_prompts(jsonl_path: Path) -> List[dict]:
    """Walk a Codex rollout; return one entry per real user prompt with the
    line offset, text, and timestamp. The line offset is what truncation needs."""
    prompts: List[dict] = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "message" or p.get("role") != "user":
                    continue
                text = _codex_extract_text(p)
                if not _codex_is_real_user_text(text):
                    continue
                prompts.append({
                    "line": i,
                    "text": text,
                    "timestamp": d.get("timestamp") or "",
                })
    except OSError:
        return []
    return prompts


def _codex_collect_assistant_text(jsonl_path: Path, user_lines: List[int]) -> Dict[int, str]:
    """For every user prompt line, gather the assistant text that follows it
    (until the next user prompt). Used so the browser's "search in replies"
    mode works for Codex too."""
    if not user_lines:
        return {}
    out: Dict[int, List[str]] = {ln: [] for ln in user_lines}
    user_lines_sorted = sorted(user_lines)
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            current_owner: Optional[int] = None
            owner_idx = -1
            for i, line in enumerate(f):
                if owner_idx + 1 < len(user_lines_sorted) and i == user_lines_sorted[owner_idx + 1]:
                    owner_idx += 1
                    current_owner = user_lines_sorted[owner_idx]
                    continue
                if current_owner is None:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "message" or p.get("role") != "assistant":
                    continue
                txt = _codex_extract_text(p)
                if txt:
                    out[current_owner].append(txt)
    except OSError:
        return {ln: "" for ln in user_lines}
    return {ln: "\n".join(parts) for ln, parts in out.items()}


def _codex_load_sessions(cwd_filter: Optional[str], all_projects: bool = False) -> Dict[str, dict]:
    """Scan all Codex rollouts; return a dict keyed by session_id with metadata,
    prompts list (with line offsets), and the source file path. When
    cwd_filter is set, restrict to sessions whose session_meta cwd matches the
    filter (descendant match) unless all_projects is True."""
    sessions: Dict[str, dict] = {}
    try:
        cwd_resolved = Path(cwd_filter).resolve() if cwd_filter else None
    except OSError:
        cwd_resolved = None
    for path in _codex_session_files():
        meta = _codex_read_session_meta(path)
        if not meta or not meta.get("id"):
            continue
        sess_cwd = meta.get("cwd") or ""
        if cwd_resolved and not all_projects:
            try:
                sp = Path(sess_cwd).resolve() if sess_cwd else None
            except OSError:
                sp = None
            if sp is None:
                continue
            if sp != cwd_resolved:
                try:
                    sp.relative_to(cwd_resolved)
                except ValueError:
                    continue
        prompts = _codex_parse_session_prompts(path)
        sessions[meta["id"]] = {
            "session_id": meta["id"],
            "session_file": str(path),
            "cwd": sess_cwd,
            "timestamp": meta.get("timestamp") or "",
            "forked_from_id": meta.get("forked_from_id"),
            "thread_source": meta.get("thread_source"),
            "originator": meta.get("originator"),
            "model_provider": meta.get("model_provider"),
            "cli_version": meta.get("cli_version"),
            "prompts": prompts,
            # Filled lazily by build_nodes when search-in-replies is on.
            "_answers": None,
        }
    return sessions


def _codex_build_nodes(sessions: Dict[str, dict]) -> Tuple[Dict[str, dict], set]:
    """Map Codex's session/prompt schema onto treeflow's node graph.

    Each user prompt becomes a node with uuid `<session_id>:<index>`. Within a
    session, prompts chain linearly (parent = previous prompt). When a session
    has `forked_from_id` pointing at another session we have nodes for, the
    forked session's first prompt's parent is approximated to the parent
    session's LAST prompt (Codex doesn't record the exact fork message).

    native_reachable = the last prompt of each session — that's where stock
    `codex resume <sid>` actually lands."""
    nodes: Dict[str, dict] = {}
    native_reachable: set = set()
    for sid, sess in sessions.items():
        prompts = sess["prompts"]
        if not prompts:
            continue
        # Lazily compute assistant text once per session (for "search in replies").
        if sess.get("_answers") is None:
            sess["_answers"] = _codex_collect_assistant_text(
                Path(sess["session_file"]), [p["line"] for p in prompts]
            )
        for idx, prompt in enumerate(prompts):
            node_id = f"{sid}:{idx}"
            if idx == 0:
                parent_id = None
                fpid = sess.get("forked_from_id")
                if fpid and fpid in sessions and sessions[fpid]["prompts"]:
                    parent_id = f"{fpid}:{len(sessions[fpid]['prompts']) - 1}"
            else:
                parent_id = f"{sid}:{idx - 1}"
            nodes[node_id] = {
                "uuid": node_id,
                "parent": parent_id,
                "children": [],
                "text": prompt["text"],
                "timestamp": prompt["timestamp"] or "",
                "session_id": sid,
                "cwd": sess.get("cwd") or "",
                "native_session_id": None,
                "answer_text": sess["_answers"].get(prompt["line"], ""),
                # Driver-specific extras so `synthesize` can find the source file.
                "_codex_session_file": sess["session_file"],
                "_codex_prompt_line": prompt["line"],
                "_codex_prompt_index": idx,
                "_codex_total_prompts": len(prompts),
            }
            if idx == len(prompts) - 1:
                native_reachable.add(node_id)
                nodes[node_id]["native_session_id"] = sid
    for nid, n in nodes.items():
        pid = n["parent"]
        if pid and pid in nodes:
            nodes[pid]["children"].append(nid)
    return nodes, native_reachable


def _codex_write_synthetic(target: dict) -> Tuple[str, int, Path]:
    """Write a new rollout file truncated so its 'last user message' is the
    chosen node. Returns (new_sid, chain_length, new_path). The synthetic file
    is placed in `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<new-sid>.jsonl`
    so codex's by-id lookup finds it."""
    src = Path(target["_codex_session_file"])
    src_lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    target_line = target["_codex_prompt_line"]
    # Find the NEXT real user prompt after target_line — cut just before it so
    # we keep the target prompt + the assistant's reply to it. If there's no
    # later user prompt, copy the whole file.
    cut: Optional[int] = None
    for i in range(target_line + 1, len(src_lines)):
        try:
            d = json.loads(src_lines[i])
        except json.JSONDecodeError:
            continue
        if d.get("type") != "response_item":
            continue
        p = d.get("payload") or {}
        if p.get("type") != "message" or p.get("role") != "user":
            continue
        if _codex_is_real_user_text(_codex_extract_text(p)):
            cut = i
            break
    chain = src_lines[:cut] if cut is not None else src_lines[:]
    if not chain:
        raise RuntimeError("synthetic rollout would be empty")
    # Rewrite the session_meta header with a fresh id + timestamp; drop any
    # forked_from_id so codex treats the new file as an independent session.
    try:
        meta = json.loads(chain[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse source session_meta: {exc}")
    new_sid = str(uuid.uuid4())
    payload = meta.get("payload") or {}
    payload["id"] = new_sid
    payload.pop("forked_from_id", None)
    now = datetime.now(timezone.utc)
    payload["timestamp"] = now.isoformat().replace("+00:00", "Z")
    meta["payload"] = payload
    meta["timestamp"] = payload["timestamp"]
    chain[0] = json.dumps(meta, ensure_ascii=False)
    out_dir = CODEX_SESSIONS_DIR / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    new_path = out_dir / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_sid}.jsonl"
    new_path.write_text("\n".join(chain) + "\n", encoding="utf-8")
    return new_sid, len(chain), new_path


def _codex_emit_resume(node: dict, exec_after: bool) -> None:
    native_sid = node.get("native_session_id") if isinstance(node, dict) else None
    if native_sid:
        cmd_str = f"codex resume {native_sid}"
        print()
        print(green(f"  ✓ native resume target — using original session, no file written"))
        print(bold(f"  $ {cmd_str}"))
        print()
        if exec_after:
            print(dim("  → exec…"))
            try:
                os.execvp("codex", ["codex", "resume", native_sid])
            except FileNotFoundError:
                print(red("  ! `codex` not found — install OpenAI Codex CLI and put it on PATH"))
                sys.exit(2)
        return

    new_sid, chain_len, new_path = _codex_write_synthetic(node)
    cmd_str = f"codex resume {new_sid}"
    print()
    print(green(f"  ✓ synthesized rollout ({chain_len}-line chain) → {new_path.name}"))
    print(bold(f"  $ {cmd_str}"))
    print()
    if exec_after:
        print(dim("  → exec…"))
        try:
            os.execvp("codex", ["codex", "resume", new_sid])
        except FileNotFoundError:
            print(red("  ! `codex` not found — install OpenAI Codex CLI and put it on PATH"))
            sys.exit(2)


# ---------- driver dispatch ----------

class ClaudeDriver:
    """Wraps the Claude-Code data model so command code can stay tool-agnostic."""
    name = "claude"
    resume_verb = "claude --resume"

    def display_project(self, cwd: str) -> Path:
        return project_dir_for(cwd)

    def gather(self, cwd: str, scope_root: Optional[str]
               ) -> Tuple[Path, Dict[str, dict], set, Any]:
        pd = project_dir_for(cwd)
        if not pd.exists():
            print(red(f"Claude Code project directory not found: {pd}"), file=sys.stderr)
            print(dim(f"  (slug = {slug_for(cwd)})"), file=sys.stderr)
            sys.exit(2)
        by_uuid, session_leaf = load_project(pd)
        nodes, native = build_nodes(by_uuid, session_leaf)
        if scope_root:
            matches = [u for u in nodes if u.startswith(scope_root)]
            if not matches:
                print(red(f"scope root uuid {scope_root!r} not found in this project"), file=sys.stderr)
                sys.exit(2)
            if len(matches) > 1:
                print(red(f"scope root prefix {scope_root!r} is ambiguous"), file=sys.stderr)
                sys.exit(2)
            keep = _subtree(matches[0], nodes)
            nodes = {u: nodes[u] for u in keep}
            native = native & keep
        # Raw bag for `emit_resume`'s synthesis step.
        return pd, nodes, native, by_uuid

    def emit_resume(self, project_dir: Path, node: dict, raw: Any, exec_after: bool) -> None:
        emit_resume(project_dir, node, raw, exec_after)

    def scan_roots(self, cwd: str, all_projects: bool) -> Tuple[List[dict], str]:
        return _scan_roots(cwd, all_projects=all_projects)


class CodexDriver:
    name = "codex"
    resume_verb = "codex resume"

    def display_project(self, cwd: str) -> Path:
        # Codex doesn't have a per-cwd directory — show the actual cwd.
        return Path(cwd)

    def gather(self, cwd: str, scope_root: Optional[str]
               ) -> Tuple[Path, Dict[str, dict], set, Any]:
        sessions = _codex_load_sessions(cwd)
        if not sessions:
            print(red(f"no Codex sessions found under cwd {cwd!r}"), file=sys.stderr)
            print(dim(f"  scanned {CODEX_SESSIONS_DIR}"), file=sys.stderr)
            sys.exit(2)
        nodes, native = _codex_build_nodes(sessions)
        if scope_root:
            matches = [u for u in nodes if u.startswith(scope_root)]
            if not matches:
                print(red(f"scope root {scope_root!r} not found"), file=sys.stderr)
                sys.exit(2)
            if len(matches) > 1:
                print(red(f"scope root prefix {scope_root!r} is ambiguous"), file=sys.stderr)
                sys.exit(2)
            keep = _subtree(matches[0], nodes)
            nodes = {u: nodes[u] for u in keep}
            native = native & keep
        return Path(cwd), nodes, native, sessions

    def emit_resume(self, project_dir: Path, node: dict, raw: Any, exec_after: bool) -> None:
        _codex_emit_resume(node, exec_after)

    def scan_roots(self, cwd: str, all_projects: bool) -> Tuple[List[dict], str]:
        sessions = _codex_load_sessions(cwd, all_projects=all_projects)
        nodes, native = _codex_build_nodes(sessions)
        roots: List[dict] = []
        for sid, sess in sessions.items():
            prompts = sess["prompts"]
            if not prompts:
                continue
            first_id = f"{sid}:0"
            last_id = f"{sid}:{len(prompts) - 1}"
            # We treat a session as a "root" for picker purposes if its first
            # prompt has no parent in the node graph (independent or its fork
            # parent fell out of scope). Forked sessions whose parent is in
            # scope show up as children inside the tree view, not in the
            # picker.
            if first_id not in nodes:
                continue
            parent = nodes[first_id].get("parent")
            if parent and parent in nodes:
                continue
            sub = _subtree(first_id, nodes)
            roots.append({
                "root_uuid": first_id,
                "project_dir": sess["session_file"],
                "cwd": sess["cwd"],
                "slug": sid,
                "title": nodes[last_id]["text"],
                "root_text": nodes[first_id]["text"],
                "latest_uuid": last_id,
                "first_seen": prompts[0]["timestamp"],
                "last_active": prompts[-1]["timestamp"],
                "node_count": len(sub),
                "native_in_subtree": sum(1 for u in sub if u in native),
                "native_at_root": first_id in native,
            })
        roots.sort(key=lambda r: r["last_active"], reverse=True)
        scope = (f"all of {CODEX_SESSIONS_DIR} ({len(sessions)} sessions)"
                 if all_projects else cwd)
        if not roots and not all_projects:
            scope = f"no Codex session at {cwd}"
        return roots, scope


_DRIVERS = {"claude": ClaudeDriver(), "codex": CodexDriver()}


def _get_driver(name: str):
    if name not in _DRIVERS:
        raise ValueError(f"unknown tool: {name!r}; choose one of {sorted(_DRIVERS)}")
    return _DRIVERS[name]


# ---------- listing / display ----------

def _format_time(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def _format_relative(ts: str) -> str:
    """Claude Code-style "21 minutes ago" / "1 week ago" rendering."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:16]
    now = datetime.now(timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        n = secs
        return f"{n} second{'s' if n != 1 else ''} ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def _highlight(text: str, query: str) -> str:
    if not query or not _USE_COLOR:
        return text
    return re.sub(re.escape(query), lambda m: yellow(m.group()), text, flags=re.IGNORECASE)


def _emit(text: str, out) -> None:
    print(text, file=out)


def render_table(items: List[dict], native_reachable: set, out, highlight: str = "") -> None:
    if not items:
        _emit(dim("(empty)"), out)
        return
    cols = shutil.get_terminal_size((100, 20)).columns
    w_idx = max(2, len(str(len(items))))
    fixed = w_idx + 2 + 11 + 2 + 8 + 2 + 1 + 2 + 1 + 2
    text_w = max(30, cols - fixed - 1)

    _emit("", out)
    head = f"{'#'.rjust(w_idx)}  {'time':<11}  {'uuid':<8}  {'L':<1}  {'★':<1}  prompt"
    _emit(dim(head), out)
    _emit(dim("─" * min(cols - 1, len(head) + text_w)), out)
    for i, n in enumerate(items, 1):
        ts = _format_time(n["timestamp"])
        uid = n["uuid"][:8]
        leaf = green("·") if not n["children"] else " "
        nat = purple("★") if n["uuid"] in native_reachable else " "
        text = re.sub(r"\s+", " ", n["text"]).strip()
        if len(text) > text_w:
            text = text[: text_w - 1] + "…"
        text = _highlight(text, highlight)
        _emit(f"{str(i).rjust(w_idx)}  {ts:<11}  {dim(uid)}  {leaf}  {nat}  {text}", out)
    _emit("", out)


def render_tree(roots: List[str], nodes: Dict[str, dict], native_reachable: set,
                out, highlight: str = "") -> None:
    """Compact-skeleton tree. Only forks + leaves + chain endpoints are shown.
       Long linear runs are collapsed to a `⋮ (N hidden)` line.

       Iterative to avoid Python's 1000-depth recursion limit on long chains.
    """
    cols = shutil.get_terminal_size((120, 20)).columns

    def sort_key(uid: str) -> str:
        return nodes[uid]["timestamp"] or ""

    def emit_line(prefix: str, glyph: str, uid: str) -> None:
        n = nodes[uid]
        ts = _format_time(n["timestamp"])
        uid_short = dim(n["uuid"][:8])
        nat = purple("★") if uid in native_reachable else " "
        leaf = green("·") if not n["children"] else " "
        text = re.sub(r"\s+", " ", n["text"]).strip()
        text = _highlight(text, highlight)
        head_len = len(prefix) + len(glyph) + 1 + 11 + 1 + 8 + 1 + 1 + 1
        avail = max(20, cols - head_len - 1)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        if len(plain) > avail:
            text = text[: avail - 1] + "…"
        _emit(f"{prefix}{glyph}{nat} {ts} {uid_short} {leaf} {text}", out)

    # Iterative DFS. Each stack entry is a node-to-process plus its visual
    # context (prefix, branch glyph, is-last-among-siblings).
    roots_sorted = sorted(roots, key=sort_key)
    for ri, root in enumerate(roots_sorted):
        if ri > 0:
            _emit("", out)
        stack: List[Tuple[str, str, str, bool]] = [(root, "", "● ", True)]
        while stack:
            uid, prefix, glyph, is_last = stack.pop()

            # Compress single-child chain starting at `uid`: walk down until a fork
            # or leaf. The "run" is [uid, ..., tail]; we only emit head + summary
            # (if long) + tail. Cap the walk so a cycle (defensive) can't loop.
            run = [uid]
            current = uid
            cap = len(nodes) + 1
            while len(nodes[current]["children"]) == 1 and cap > 0:
                cap -= 1
                child = nodes[current]["children"][0]
                if child in run:  # defensive cycle guard
                    break
                run.append(child)
                current = child
            tail = run[-1]
            run_len = len(run)

            cont_prefix = prefix + ("   " if is_last else "│  ")
            if run_len == 1:
                emit_line(prefix, glyph, uid)
            elif run_len == 2:
                emit_line(prefix, glyph, uid)
                emit_line(cont_prefix, "└─ ", tail)
            else:
                emit_line(prefix, glyph, uid)
                _emit(f"{cont_prefix}{dim(f'⋮ ({run_len - 2} hidden)')}", out)
                emit_line(cont_prefix, "└─ ", tail)

            # The tail's children continue the tree. They live one level deeper
            # than the tail (which was rendered with └─ at cont_prefix).
            tail_children = sorted(nodes[tail]["children"], key=sort_key)
            child_prefix = cont_prefix + ("   " if run_len > 1 else "")
            n_kids = len(tail_children)
            # Push reversed so first child pops first (DFS, left-to-right order).
            for i in range(n_kids - 1, -1, -1):
                child_last = (i == n_kids - 1)
                child_glyph = "└─ " if child_last else "├─ "
                stack.append((tail_children[i], child_prefix, child_glyph, child_last))


def with_pager(render_fn) -> str:
    """Render to a string buffer; pipe through less -RFX when stdout is tty and content
    is long; otherwise just print. Returns the rendered text for callers that need it."""
    buf = io.StringIO()
    render_fn(buf)
    text = buf.getvalue()
    if not sys.stdout.isatty():
        sys.stdout.write(text)
        return text
    rows = shutil.get_terminal_size((80, 24)).lines
    line_count = text.count("\n")
    if line_count < rows - 3:
        sys.stdout.write(text)
        return text
    try:
        proc = subprocess.Popen(
            ["less", "-R", "-F", "-X"],
            stdin=subprocess.PIPE,
        )
        proc.communicate(text.encode("utf-8"))
    except (FileNotFoundError, BrokenPipeError):
        sys.stdout.write(text)
    return text


_NODE_PAGE = int(_cfg("page_size", default=15))
_VIEW_CYCLE = ("tree", "list", "leaves")


def _flatten_tree(roots: List[str], nodes: Dict[str, dict],
                  reverse: bool = False) -> List[Tuple[str, str, str, bool]]:
    """Iterative depth-first walk that mirrors render_tree's compact collapsing.

    Yields (uuid_or_None, prefix, branch_glyph, is_last). When uuid is None the
    entry is the "⋮ N hidden" filler — not selectable. Linear single-child runs
    are collapsed so a 1700-node tree renders in a screenful of structural rows.

    `reverse=False`: classic top-down view, sibling sets oldest-first.

    `reverse=True`: sibling sets are sorted by the **latest timestamp anywhere
    in their subtree**, ascending — so the subtree containing the absolute
    newest leaf is visited LAST in the DFS. Callers reverse the returned list
    afterwards to put that newest leaf at the top of the screen with the root
    at the bottom; sorting by subtree-max-ts means "newest" wins over "deepest".
    """
    out: List[Tuple[Optional[str], str, str, bool]] = []

    if reverse:
        # Iterative post-order so we don't hit recursion limit on deep trees.
        subtree_max: Dict[str, str] = {}
        post_stack: List[Tuple[str, bool]] = [(r, False) for r in roots]
        while post_stack:
            uid, done = post_stack.pop()
            if done:
                best = nodes[uid]["timestamp"] or ""
                for c in nodes[uid]["children"]:
                    cm = subtree_max.get(c, "")
                    if cm > best:
                        best = cm
                subtree_max[uid] = best
            else:
                post_stack.append((uid, True))
                for c in nodes[uid]["children"]:
                    post_stack.append((c, False))

        def sort_key(uid: str) -> str:
            return subtree_max.get(uid, nodes[uid]["timestamp"] or "")
    else:
        def sort_key(uid: str) -> str:
            return nodes[uid]["timestamp"] or ""

    # In reverse mode the caller flips the whole list afterwards, so a
    # node we now emit with `└─` will end up visually ABOVE its parent —
    # the corner needs to point down-right instead of up-right. Swap to
    # `┌─` here so the rendered tree reads correctly bottom-up.
    last_glyph = "┌─ " if reverse else "└─ "

    roots_sorted = sorted(roots, key=sort_key)
    for ri, root in enumerate(roots_sorted):
        if ri > 0:
            out.append((None, "", "", True))  # blank separator between roots
        stack: List[Tuple[str, str, str, bool]] = [(root, "", "● ", True)]
        while stack:
            uid, prefix, glyph, is_last = stack.pop()
            run = [uid]
            current = uid
            cap = len(nodes) + 1
            while len(nodes[current]["children"]) == 1 and cap > 0:
                cap -= 1
                child = nodes[current]["children"][0]
                if child in run:
                    break
                run.append(child)
                current = child
            tail = run[-1]
            run_len = len(run)
            cont_prefix = prefix + ("   " if is_last else "│  ")
            if run_len == 1:
                out.append((uid, prefix, glyph, is_last))
            elif run_len == 2:
                out.append((uid, prefix, glyph, is_last))
                out.append((tail, cont_prefix, last_glyph, True))
            else:
                out.append((uid, prefix, glyph, is_last))
                out.append((None, cont_prefix, f"⋮ ({run_len - 2} hidden)", True))
                out.append((tail, cont_prefix, last_glyph, True))

            tail_children = sorted(nodes[tail]["children"], key=sort_key)
            child_prefix = cont_prefix + ("   " if run_len > 1 else "")
            n_kids = len(tail_children)
            for i in range(n_kids - 1, -1, -1):
                child_last = (i == n_kids - 1)
                child_glyph = last_glyph if child_last else "├─ "
                stack.append((tail_children[i], child_prefix, child_glyph, child_last))
    return out  # type: ignore[return-value]


def _browse_render(state: dict, items: List[dict], all_nodes: Dict[str, dict],
                   native: set, project_dir: Path, scope_summary: str) -> int:
    """Render the unified browser screen. Returns line count for menu_clear."""
    cols = shutil.get_terminal_size((140, 24)).columns
    view = state["view"]
    search = state["search"]
    selected = state["selected"]
    offset = state["offset"]

    leaf_total = sum(1 for n in all_nodes.values() if not n["children"])
    proj_str = _truncate_w(str(project_dir), cols - 14)
    lines: List[str] = [
        bold("treeflow") + dim("  ") + dim(_truncate_w(scope_summary, cols - 6)),
        dim(f"  {proj_str}"),
        (dim("  nodes ") + bold(str(len(all_nodes)))
         + dim(" · leaves ") + bold(str(leaf_total))
         + dim(" · native ") + purple(f"★ {len(native)}")
         + dim(" · treeflow-only ") + str(len(all_nodes) - len(native))),
    ]
    # search bar — always visible, cursor block at end
    cursor = green("▌")
    search_show = search if search else dim("(type to filter)")
    scope = state.get("search_scope", "prompts")
    scope_label = "in prompts" if scope == "prompts" else "in prompts + replies"
    lines.append("")
    count_part = f" ({len(items)} match · {scope_label})" if search else f" ({len(items)} total · {scope_label})"
    lines.append(f"  {bold('search:')} {search_show}{cursor}  " + dim(count_part))
    # view tabs
    tabs = []
    for v in _VIEW_CYCLE:
        if v == view:
            tabs.append(green("[") + bold(v) + green("]"))
        else:
            tabs.append(dim(f" {v} "))
    lines.append("  " + "  ".join(tabs) + dim("   (⇥ next view)"))
    extras = []
    if view == "tree":
        extras.append("^R flip")
    extras.append("^A scope")
    extras_hint = " · " + " · ".join(extras) if extras else ""
    lines.append(dim(f"  ↑↓ move · ⏎ resume{extras_hint} · ⎋ back · ^C quit"))
    lines.append("")

    if not items:
        lines.append(dim("  (no nodes match)"))
        for line in lines:
            print(line)
        return len(lines)

    # render body based on view
    rows = shutil.get_terminal_size((80, 24)).lines
    body_capacity = max(5, rows - len(lines) - 4)
    page_size = min(_NODE_PAGE, body_capacity)

    if view == "tree":
        # Items here is the flattened tree (list of dicts each containing
        # uuid / prefix / glyph / is_filler / node). selected = index into
        # selectable (non-filler) entries.
        page = items[offset:offset + page_size]
        for line_entry in page:
            abs_idx = items.index(line_entry)  # could be O(n) but page is small
            n = line_entry.get("node")
            if line_entry["filler"]:
                lines.append(f"  {line_entry['prefix']}{dim(line_entry['glyph'])}")
                continue
            ts = _format_time(n["timestamp"])
            uid_short = dim(n["uuid"][:8])
            nat = purple("★") if n["uuid"] in native else " "
            leaf = green("·") if not n["children"] else " "
            text = re.sub(r"\s+", " ", n["text"]).strip()
            text_w = max(15, cols - len(line_entry["prefix"]) - len(line_entry["glyph"]) - 30)
            text = _truncate_w(text, text_w)
            text = _highlight(text, search)
            line_str = (f"{line_entry['prefix']}{line_entry['glyph']}{nat} "
                        f"{dim(ts)} {uid_short} {leaf} {text}")
            if abs_idx == selected:
                lines.append("▶ " + line_str)
            else:
                lines.append("  " + line_str)
        if len(items) > page_size:
            lines.append(dim(f"    … showing {offset + 1}-{offset + len(page)} "
                             f"of {len(items)}"))
    else:
        # list / leaves: flat paginated table
        num_w = max(2, len(str(len(items))))
        head_w = 4 + (num_w + 1) + 1 + 11 + 2 + 8 + 2 + 1 + 2 + 1 + 2
        text_w = max(15, cols - head_w - 2)
        lines.append(dim(
            f"  {'#'.rjust(num_w + 1)}  {'time':<11}  {'uuid':<8}  L  ★  prompt"
        ))
        lines.append(dim("─" * min(cols - 1, head_w + min(text_w, 60))))
        page = items[offset:offset + page_size]
        for i, n in enumerate(page):
            abs_idx = offset + i
            num_str = f"{abs_idx + 1:>{num_w}}."
            ts = _format_time(n["timestamp"])
            uid = n["uuid"][:8]
            leaf = green("·") if not n["children"] else " "
            nat = purple("★") if n["uuid"] in native else " "
            text = re.sub(r"\s+", " ", n["text"]).strip()
            text = _truncate_w(text, text_w)
            text = _highlight(text, search)
            if abs_idx == selected:
                arrow = green("▶")
                row = (f"  {arrow} {bold(num_str)}  {dim(ts)}  {dim(uid)}  "
                       f"{leaf}  {nat}  {bold(text)}")
            else:
                row = (f"    {dim(num_str)}  {dim(ts)}  {dim(uid)}  "
                       f"{leaf}  {nat}  {text}")
            lines.append(row)
        if len(items) > page_size:
            lines.append(dim(f"    … showing {offset + 1}-{offset + len(page)} "
                             f"of {len(items)}"))

    for line in lines:
        print(line)
    return len(lines)


def _compute_browse_items(state: dict, all_nodes: Dict[str, dict]) -> List:
    """Return the items list to render given the current view + search.

    For tree view the items are flattened tree-line dicts; for list/leaves they
    are raw node dicts. Selection indexes into the returned list."""
    view = state["view"]
    search = state["search"].lower()
    scope = state.get("search_scope", "prompts")  # "prompts" | "all"

    def node_matches(n: dict) -> bool:
        if not search:
            return True
        if search in (n["text"] or "").lower():
            return True
        if scope == "all" and search in (n.get("answer_text") or "").lower():
            return True
        return False

    if view == "tree":
        roots = [u for u, n in all_nodes.items()
                 if not (n["parent"] and n["parent"] in all_nodes)]
        reverse = state.get("tree_reverse", False)
        flat = _flatten_tree(roots, all_nodes, reverse=reverse)
        if reverse:
            # Flip the whole list so the newest leaf — the last node visited
            # by the subtree-max-ts ordered DFS — lands at the top.
            flat = list(reversed(flat))
        items = []
        for uuid_, prefix, glyph, _is_last in flat:
            items.append({
                "uuid": uuid_,
                "node": all_nodes[uuid_] if uuid_ else None,
                "prefix": prefix,
                "glyph": glyph,
                "filler": uuid_ is None,
            })
        if search:
            items = [it for it in items
                     if (not it["filler"]) and node_matches(it["node"])]
        return items

    if view == "leaves":
        nodes_list = [n for n in all_nodes.values() if not n["children"]]
    else:  # list
        nodes_list = list(all_nodes.values())
    nodes_list.sort(key=lambda n: n["timestamp"] or "", reverse=True)
    if search:
        nodes_list = [n for n in nodes_list if node_matches(n)]
    return nodes_list


def _render_node_detail(node: dict, driver: Any, native_set: set,
                        project_dir: Path) -> None:
    """Print the full detail view for a node — both halves of the turn plus
    the resume command. Runs inside the alternate screen buffer, so we can
    print as much as we want without worrying about preserving the layout."""
    cols = shutil.get_terminal_size((140, 24)).columns
    uid = node["uuid"]
    is_native = uid in native_set
    native_sid = node.get("native_session_id")
    reply = (node.get("answer_text") or "").rstrip()
    prompt = (node.get("text") or "").rstrip()

    print(bold("treeflow") + dim(" · node detail"))
    print(dim("─" * min(cols, 100)))
    print(f"  {bold('uuid')      :<18}  {uid}")
    print(f"  {bold('time')      :<18}  {_format_time(node['timestamp'])}"
          + dim(f"   ({node['timestamp'] or '—'})"))
    print(f"  {bold('session')   :<18}  {dim(node['session_id'] or '—')}")
    if node.get("cwd"):
        print(f"  {bold('cwd')   :<18}  {dim(node['cwd'])}")
    print(f"  {bold('native')    :<18}  "
          + (purple('★ yes') if is_native else dim('no')))
    print(f"  {bold('parent')    :<18}  {dim(node['parent'] or '(root)')}")
    print(f"  {bold('children')  :<18}  {len(node['children'])}")
    print()

    print(bold("── user prompt ") + dim("─" * max(0, min(cols, 100) - 16)))
    print(prompt if prompt else dim("(empty)"))
    print()

    print(bold("── assistant reply ") + dim("─" * max(0, min(cols, 100) - 20)))
    if reply:
        print(reply)
    else:
        print(dim("(no recorded assistant reply for this node)"))
    print()

    print(bold("── resume ") + dim("─" * max(0, min(cols, 100) - 11)))
    if native_sid:
        cmd = f"{driver.resume_verb} {native_sid}"
        print(f"  {bold('$')} {cmd}")
        print(dim("  native target — stock `--resume` already lands here, "
                  "no synthetic file needed"))
    else:
        print(dim(f"  This node is on an abandoned branch. treeflow will "
                  f"write a fresh\n  synthetic session file and run:"))
        print()
        print(f"  {bold('$')} {driver.resume_verb} {dim('<new-session-id>')}")
    print()
    print(dim("  ⏎ resume · ← / ⎋ back · ^C quit"))


def _show_node_detail(node: dict, raw: "_RawInput", driver: Any,
                      native_set: set, project_dir: Path) -> Optional[object]:
    """Detail screen for one picked node. Uses the alternate screen buffer so
    long prompts/replies render cleanly without clobbering the browser layout
    underneath. Returns BACK (go back to browser), True (proceed to resume),
    or None (quit)."""
    # Enter alt screen + clear + cursor home.
    sys.stdout.write("\x1b[?1049h\x1b[H\x1b[2J")
    sys.stdout.flush()
    try:
        while True:
            sys.stdout.write("\x1b[H\x1b[2J")
            sys.stdout.flush()
            _render_node_detail(node, driver, native_set, project_dir)
            try:
                key = raw.read_key()
            except KeyboardInterrupt:
                return None
            if key in ("left", "esc", "backspace", "q", "Q"):
                return BACK
            if key == "enter":
                return True
            # Any other key: just redraw (no state change in the detail view).
    finally:
        # Leave alt screen — the browser underneath is restored exactly.
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()


def browse_project(project_dir: Path, all_nodes: Dict[str, dict], native: set,
                   scope_summary: str = "", driver: Any = None
                   ) -> Optional[object]:
    """The unified post-project browser. Combines view-mode switching with a
    live search box. Returns either a node dict (to resume), BACK (go back to
    project picker), or None (quit)."""
    if not all_nodes or not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    state = {
        "view": _cfg("browser", "default_view", default="tree"),
        "search": "",
        "search_scope": "prompts",  # "prompts" | "all" (prompts + assistant replies)
        "selected": 0,
        "offset": 0,
        "tree_reverse": _cfg("browser", "tree_order", default="oldest_first") == "newest_first",
    }

    items = _compute_browse_items(state, all_nodes)
    drawn = _browse_render(state, items, all_nodes, native, project_dir, scope_summary)

    def refresh(keep_selection: bool = False) -> None:
        nonlocal items, drawn
        items = _compute_browse_items(state, all_nodes)
        if not keep_selection or state["selected"] >= len(items):
            state["selected"] = 0
            state["offset"] = 0
        _menu_clear(drawn)
        drawn = _browse_render(state, items, all_nodes, native, project_dir, scope_summary)

    with _RawInput() as raw:
        while True:
            try:
                key = raw.read_key()
            except KeyboardInterrupt:
                _menu_clear(drawn)
                return None

            if key == "enter":
                if items:
                    entry = items[state["selected"]]
                    node = entry.get("node") if isinstance(entry, dict) and "filler" in entry else entry
                    if node and driver is not None:
                        # Pop into the detail view. If the user backs out we
                        # come straight back to the same browser screen with
                        # selection preserved; if they confirm, return the
                        # node up to main() to do the resume.
                        decision = _show_node_detail(node, raw, driver, native, project_dir)
                        if decision is None:
                            _menu_clear(drawn)
                            return None
                        if decision is BACK:
                            continue
                        # decision is True → resume this node.
                        _menu_clear(drawn)
                        return node
                    elif node:
                        # No driver provided (e.g. test) — keep legacy behavior.
                        _menu_clear(drawn)
                        return node
            elif key == "tab":
                idx = _VIEW_CYCLE.index(state["view"])
                state["view"] = _VIEW_CYCLE[(idx + 1) % len(_VIEW_CYCLE)]
                refresh()
            elif key == "shift-tab":
                idx = _VIEW_CYCLE.index(state["view"])
                state["view"] = _VIEW_CYCLE[(idx - 1) % len(_VIEW_CYCLE)]
                refresh()
            elif key == "\x12":  # Ctrl+R — flip tree order (only meaningful in tree view)
                if state["view"] == "tree":
                    state["tree_reverse"] = not state["tree_reverse"]
                    refresh()
            elif key == "\x01":  # Ctrl+A — toggle search scope (prompts ↔ prompts+replies)
                state["search_scope"] = "all" if state["search_scope"] == "prompts" else "prompts"
                refresh(keep_selection=True)
            elif key == "esc":
                if state["search"]:
                    state["search"] = ""
                    refresh()
                else:
                    _menu_clear(drawn)
                    return BACK
            elif key == "backspace":
                if state["search"]:
                    state["search"] = state["search"][:-1]
                    refresh()
                else:
                    _menu_clear(drawn)
                    return BACK
            elif key == "down":
                if items:
                    state["selected"] = min(state["selected"] + 1, len(items) - 1)
                    if state["selected"] >= state["offset"] + _NODE_PAGE:
                        state["offset"] = state["selected"] - _NODE_PAGE + 1
                    _menu_clear(drawn)
                    drawn = _browse_render(state, items, all_nodes, native, project_dir, scope_summary)
            elif key == "up":
                if items:
                    state["selected"] = max(state["selected"] - 1, 0)
                    if state["selected"] < state["offset"]:
                        state["offset"] = state["selected"]
                    _menu_clear(drawn)
                    drawn = _browse_render(state, items, all_nodes, native, project_dir, scope_summary)
            elif len(key) == 1 and key.isprintable():
                state["search"] += key
                refresh()
            # any other key (left, right, unknown, etc.) — ignore silently


# ---------- commands ----------

def _print_summary(project_dir: Path, total: int, native: int, leaves: int) -> None:
    print(dim(f"project dir: {project_dir}"))
    print(
        dim("nodes total: ") + bold(str(total))
        + dim("   leaves: ") + bold(str(leaves))
        + dim("   native: ") + purple(f"★ {native}")
        + dim(f"   treeflow-only: ") + str(total - native)
    )


def _driver_for(args) -> Any:
    """Resolve which driver this invocation uses. Precedence:
    CLI flag (`--codex`/`--claude`/`--tool`) > config file > built-in default."""
    name = getattr(args, "tool", None) or _cfg("tool", default="claude")
    return _get_driver(name)


def _gather(args, path: str, scope_root: Optional[str] = None
            ) -> Tuple[Path, Dict[str, dict], set, Any]:
    """Driver-aware loader. The returned `raw` is whatever the driver's
    `gather` returns as its fourth element (by_uuid for Claude, sessions for
    Codex) — only `emit_resume` uses it."""
    driver = _driver_for(args)
    return driver.gather(path, scope_root)


def _show_items(args, items: List[dict], nodes: Dict[str, dict], native: set,
                project_dir: Path, highlight: str = "") -> None:
    """Print summary + body (table or tree). Tree mode shows the full subtree
    rooted at any item whose parent is missing from `items` — for default browse
    that means full project tree; for filtered modes (leaves / search) we still
    show the proper structural tree containing each match, with non-matching
    ancestors shown so the user can see context.
    """
    tree_mode = getattr(args, "tree", False)
    show_filter = getattr(args, "leaves", False) or bool(getattr(args, "query", ""))

    def render(out):
        _emit(dim(f"project dir: {project_dir}"), out)
        leaf_count = sum(1 for n in nodes.values() if not n["children"])
        _emit(
            dim("nodes total: ") + bold(str(len(nodes)))
            + dim("   leaves: ") + bold(str(leaf_count))
            + dim("   native: ") + purple(f"★ {len(native)}")
            + dim("   treeflow-only: ") + str(len(nodes) - len(native)),
            out,
        )
        if highlight:
            _emit(dim("query: ") + bold(repr(highlight)) + dim("   matches: ")
                  + bold(str(len(items))), out)

        if tree_mode:
            # In tree mode we render the WHOLE project tree (the user explicitly
            # asked for tree visualization — they want the structure). When
            # there's a filter, dim non-matching nodes via highlight on matches.
            roots = [u for u, n in nodes.items() if not (n["parent"] and n["parent"] in nodes)]
            render_tree(roots, nodes, native, out, highlight=highlight)
        else:
            render_table(items, native, out, highlight=highlight)

    with_pager(render)


def cmd_browse(args):
    """CLI dump for `treeflow list` / `treeflow leaves`. Pure print, no interactive
    picker — the picker is reached via bare `treeflow` which opens browse_project."""
    pd, nodes, native, _raw = _gather(args, args.path, getattr(args, "scope_root", None))
    items = list(nodes.values())
    if args.leaves:
        items = [n for n in items if not n["children"]]
    items.sort(key=lambda n: n["timestamp"], reverse=True)
    if args.limit:
        items = items[: args.limit]

    if getattr(args, "json", False):
        _emit_node_listing(pd, nodes, native, items)
        return None

    _show_items(args, items, nodes, native, pd)
    return None


def cmd_search(args):
    pd, nodes, native, _raw = _gather(args, args.path, getattr(args, "scope_root", None))
    q = args.query.lower()
    matches = [n for n in nodes.values() if q in n["text"].lower()]
    matches.sort(key=lambda n: n["timestamp"], reverse=True)
    if args.limit:
        matches = matches[: args.limit]

    if getattr(args, "json", False):
        _emit_node_listing(pd, nodes, native, matches, query=args.query)
        return None

    _show_items(args, matches, nodes, native, pd, highlight=args.query)
    return None


def cmd_tree(args) -> None:
    """Dedicated tree-view command. Always tree, never pick."""
    pd, nodes, native, _raw = _gather(args, args.path, getattr(args, "scope_root", None))

    if getattr(args, "json", False):
        _emit_tree_json(pd, nodes, native)
        return

    def render(out):
        _emit(dim(f"project dir: {pd}"), out)
        leaf_count = sum(1 for n in nodes.values() if not n["children"])
        _emit(
            dim("nodes total: ") + bold(str(len(nodes)))
            + dim("   leaves: ") + bold(str(leaf_count))
            + dim("   native: ") + purple(f"★ {len(native)}")
            + dim("   treeflow-only: ") + str(len(nodes) - len(native)),
            out,
        )
        _emit("", out)
        roots = [u for u, n in nodes.items() if not (n["parent"] and n["parent"] in nodes)]
        render_tree(roots, nodes, native, out, highlight=args.match or "")

    with_pager(render)


def cmd_resume(args) -> None:
    driver = _driver_for(args)
    pd, nodes, _native, raw = _gather(args, args.path, getattr(args, "scope_root", None))

    if isinstance(driver, ClaudeDriver):
        # Claude: prefix-match against the raw event bag so non-user-prompt
        # uuids still resolve (with a warning) for backwards compatibility.
        by_uuid = raw
        candidates = [u for u in by_uuid if u.startswith(args.uuid)]
    else:
        candidates = [u for u in nodes if u.startswith(args.uuid)]

    if not candidates:
        _fail(f"no uuid starts with {args.uuid!r}", args)
    if len(candidates) > 1:
        if getattr(args, "json", False):
            _emit_json({
                "error": "ambiguous_prefix",
                "prefix": args.uuid,
                "matches": [{"uuid": m, "text": (nodes.get(m, {}).get("text") or "")[:80]}
                            for m in candidates[:20]],
            })
            sys.exit(2)
        print(red(f"prefix {args.uuid!r} is ambiguous ({len(candidates)} matches):"), file=sys.stderr)
        for m in candidates[:6]:
            text = (nodes.get(m, {}).get("text") or "")[:60]
            print(f"  {m}  {dim(text)}", file=sys.stderr)
        sys.exit(2)

    target = candidates[0]
    node = nodes.get(target)
    if node is None:
        if isinstance(driver, ClaudeDriver):
            text = _user_text((raw[target][0].get("message") or {}).get("content"))[:80]
            node = {"uuid": target, "text": text}
            if not getattr(args, "json", False):
                print(yellow(f"⚠ {target[:8]} is not a user prompt ({raw[target][0].get('type')}) — trying anyway"), file=sys.stderr)
        else:
            _fail(f"node {target!r} is not a resumable Codex prompt", args)

    if getattr(args, "json", False):
        # JSON mode mirrors the human path: native shortcut OR synthesize. We
        # call the driver's pure helpers directly so we can capture the
        # session id / file path without calling exec.
        native_sid = node.get("native_session_id") if isinstance(node, dict) else None
        if native_sid:
            cmd_str = f"{driver.resume_verb} {native_sid}"
            _emit_json({
                "tool": driver.name,
                "target_uuid": target,
                "target_text": node.get("text", ""),
                "session_id": native_sid,
                "file": None,
                "synthesized": False,
                "command": cmd_str,
            })
            return
        if isinstance(driver, ClaudeDriver):
            new_sid, chain_len, new_path = write_synthetic_session(pd, target, raw)
        else:
            new_sid, chain_len, new_path = _codex_write_synthetic(node)
        cmd_str = f"{driver.resume_verb} {new_sid}"
        _emit_json({
            "tool": driver.name,
            "target_uuid": target,
            "target_text": node.get("text", ""),
            "session_id": new_sid,
            "file": str(new_path),
            "synthesized": True,
            "chain_length": chain_len,
            "command": cmd_str,
        })
        return

    driver.emit_resume(pd, node, raw, exec_after=bool(getattr(args, "exec_", False)))


def cmd_info(args) -> None:
    pd, nodes, native, _raw = _gather(args, args.path, getattr(args, "scope_root", None))
    matches = [u for u in nodes if u.startswith(args.uuid)]
    if not matches:
        _fail(f"no uuid starts with {args.uuid!r}", args)
    if len(matches) > 1:
        _fail(f"prefix {args.uuid!r} is ambiguous", args,
              extra={"matches": matches[:20]})
    n = nodes[matches[0]]
    if getattr(args, "json", False):
        _emit_json(_node_to_dict(n, native, full_text=True))
        return
    print()
    print(bold("uuid       ") + n["uuid"])
    print(bold("session    ") + n["session_id"])
    print(bold("time       ") + _format_time(n["timestamp"]))
    print(bold("cwd        ") + (n["cwd"] or dim("—")))
    print(bold("parent     ") + (n["parent"] or dim("(root)")))
    print(bold("children   ") + str(len(n["children"])))
    print(bold("native     ") + (purple("★ yes") if n["uuid"] in native else "no"))
    print()
    print(bold("prompt"))
    print()
    for line in n["text"].splitlines():
        print(f"  {line}")
    print()


def cmd_roots(args) -> None:
    """List the root conversations under a project path. Designed for agents:
    pair with `--json` to get a machine-readable inventory; pair with
    `--all` to scan every project dir."""
    driver = _driver_for(args)
    roots, scope = driver.scan_roots(args.path or os.getcwd(),
                                     all_projects=getattr(args, "all_projects", False))
    if getattr(args, "json", False):
        _emit_json({
            "scope": scope,
            "count": len(roots),
            "roots": [
                {
                    "uuid": r["root_uuid"],
                    "uuid_short": r["root_uuid"][:8],
                    "project_dir": r["project_dir"],
                    "cwd": r["cwd"],
                    "first_seen": r["first_seen"],
                    "last_active": r["last_active"],
                    "node_count": r["node_count"],
                    "native_at_root": r["native_at_root"],
                    "native_in_subtree": r["native_in_subtree"],
                    "text": r["title"],               # latest prompt in subtree
                    "root_text": r["root_text"],      # original parentUuid:null prompt
                    "latest_uuid": r["latest_uuid"],  # uuid of the latest node
                }
                for r in roots
            ],
        })
        return
    print(dim(f"scope: {scope}"))
    print(dim("roots: ") + bold(str(len(roots))))
    print()
    for i, r in enumerate(roots, 1):
        nat = purple("★ ") if r["native_at_root"] else "  "
        ts = _format_relative(r["last_active"])
        title = re.sub(r"\s+", " ", r["title"]).strip()
        nodes_label = "node " if r["node_count"] == 1 else "nodes"
        nodes_str = f"{r['node_count']:>4} {nodes_label}"
        print(f"  {i:>3}. {nat}{dim(r['root_uuid'][:8])}  {dim(ts):<14}  "
              f"{dim(nodes_str)}  {title[:100]}")


# ---------- JSON / serialization helpers ----------

def _emit_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _fail(msg: str, args, extra: Optional[dict] = None) -> None:
    """Error exit. JSON mode emits a structured error to stdout; otherwise
    prints to stderr. Always exits 2."""
    if getattr(args, "json", False):
        payload = {"error": msg}
        if extra:
            payload.update(extra)
        _emit_json(payload)
    else:
        print(red(msg), file=sys.stderr)
    sys.exit(2)


def _node_to_dict(n: dict, native: set, full_text: bool = False) -> dict:
    text = n["text"] if full_text else re.sub(r"\s+", " ", n["text"]).strip()[:200]
    return {
        "uuid": n["uuid"],
        "uuid_short": n["uuid"][:8],
        "timestamp": n["timestamp"],
        "session_id": n["session_id"],
        "cwd": n["cwd"],
        "parent_uuid": n["parent"],
        "child_uuids": list(n["children"]),
        "is_leaf": not n["children"],
        "native_reachable": n["uuid"] in native,
        "text": text,
    }


def _emit_node_listing(pd: Path, nodes: dict, native: set,
                       items: List[dict], query: str = "") -> None:
    leaf_count = sum(1 for n in nodes.values() if not n["children"])
    payload = {
        "project_dir": str(pd),
        "stats": {
            "total": len(nodes),
            "leaves": leaf_count,
            "native": len(native),
            "treeflow_only": len(nodes) - len(native),
        },
        "count": len(items),
        "items": [_node_to_dict(n, native) for n in items],
    }
    if query:
        payload["query"] = query
    _emit_json(payload)


def _emit_tree_json(pd: Path, nodes: dict, native: set) -> None:
    def to_node(uuid_: str) -> dict:
        n = nodes[uuid_]
        return {
            "uuid": uuid_,
            "uuid_short": uuid_[:8],
            "timestamp": n["timestamp"],
            "is_leaf": not n["children"],
            "native_reachable": uuid_ in native,
            "text": re.sub(r"\s+", " ", n["text"]).strip()[:200],
            "children": [to_node(c) for c in n["children"]],
        }
    roots = [u for u, n in nodes.items() if not (n["parent"] and n["parent"] in nodes)]
    _emit_json({
        "project_dir": str(pd),
        "stats": {
            "total": len(nodes),
            "leaves": sum(1 for n in nodes.values() if not n["children"]),
            "native": len(native),
        },
        "roots": [to_node(r) for r in roots],
    })


# ---------- arg parsing ----------



def _add_common_view(p: argparse.ArgumentParser, default_limit: int = 0) -> None:
    p.add_argument("-n", "--limit", type=int, default=default_limit,
                   help="max results (default 0 = no limit, scrollable)")
    p.add_argument("-t", "--tree", action="store_true",
                   help="render as ASCII tree instead of table (overrides pick)")


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true",
                   help="JSON output (machine-readable; suppresses picker)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="treeflow",
        description="treeflow — browse and resume any node in your local Claude "
                    "Code / Codex CLI conversation tree, including abandoned branches.",
    )
    ap.add_argument("-p", "--path", default=None,
                    help="project path (default: pick interactively or use cwd)")
    ap.add_argument("-r", "--root", default=None, dest="scope_root",
                    help="scope to a specific root conversation (uuid prefix)")
    ap.add_argument("-a", "--all", action="store_true", dest="all_projects",
                    help="include roots from every project dir, "
                         "not just the one matching cwd")
    ap.add_argument("-x", "--exec", action="store_true", dest="exec_",
                    help="after picking a node (or resolving `resume`), "
                         "exec the agent CLI on the resume target instead of "
                         "just printing the command")
    tool_group = ap.add_mutually_exclusive_group()
    tool_group.add_argument("--tool", default=None, choices=sorted(_DRIVERS),
                            help="which agent CLI to target (default from config)")
    tool_group.add_argument("--claude", dest="tool", action="store_const",
                            const="claude", help="shortcut for --tool claude")
    tool_group.add_argument("--codex", dest="tool", action="store_const",
                            const="codex", help="shortcut for --tool codex")
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", aliases=["ls"], help="dump every node in a table (newest first)")
    p_list.add_argument("-L", "--leaves", action="store_true", help="only branch tails")
    _add_common_view(p_list)
    _add_json_flag(p_list)
    p_list.set_defaults(func=cmd_browse, leaves=False, tree=False)

    p_leaves = sub.add_parser("leaves", help="dump only leaf nodes (branch tails)")
    _add_common_view(p_leaves)
    _add_json_flag(p_leaves)
    p_leaves.set_defaults(func=cmd_browse, leaves=True, tree=False)

    p_search = sub.add_parser("search", aliases=["s"], help="dump prompts matching a substring")
    p_search.add_argument("query")
    _add_common_view(p_search)
    _add_json_flag(p_search)
    p_search.set_defaults(func=cmd_search, tree=False)

    p_tree = sub.add_parser("tree", aliases=["t"], help="ASCII tree of the whole project")
    p_tree.add_argument("-m", "--match", default="",
                        help="highlight prompts matching this substring")
    _add_json_flag(p_tree)
    p_tree.set_defaults(func=cmd_tree)

    p_roots = sub.add_parser("roots", help="list root conversations in this project")
    _add_json_flag(p_roots)
    p_roots.set_defaults(func=cmd_roots)

    p_resume = sub.add_parser("resume", aliases=["r"], help="generate resume command for a uuid prefix")
    p_resume.add_argument("uuid")
    # Subparser-level `-x` keeps working for back-compat (`treeflow resume <uuid> -x`).
    # Use SUPPRESS so the top-level value persists when -x is only at the top.
    p_resume.add_argument("-x", "--exec", action="store_true", dest="exec_",
                          default=argparse.SUPPRESS)
    _add_json_flag(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_info = sub.add_parser("info", aliases=["i"], help="show full prompt for a node")
    p_info.add_argument("uuid")
    _add_json_flag(p_info)
    p_info.set_defaults(func=cmd_info)

    return ap


# ---------- conversation-root picker ----------
#
# A "project" here matches the Flask app's meaning: one root user prompt
# (parentUuid: null) plus the subtree growing from it. A single
# ~/.claude/projects/<slug>/ directory can contain many such roots — they're
# the independent conversations the user started while cd'd into that path.

def _subtree(root_uuid: str, nodes: Dict[str, dict]) -> set:
    """BFS the user-prompt children of root_uuid; return the inclusive uuid set."""
    out: set = set()
    stack = [root_uuid]
    while stack:
        u = stack.pop()
        if u in out or u not in nodes:
            continue
        out.add(u)
        stack.extend(nodes[u].get("children", []))
    return out


def _dir_real_cwd(project_dir: Path) -> str:
    """Recover the original `cwd` recorded in any jsonl message — the slug is
    lossy (slashes and underscores both collapse to '-') so we can't unslug."""
    try:
        jsonls = sorted(project_dir.glob("*.jsonl"),
                        key=lambda x: x.stat().st_mtime, reverse=True)
    except OSError:
        return project_dir.name
    for j in jsonls:
        try:
            with j.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s.startswith("{"):
                        continue
                    try:
                        data = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    if data.get("cwd"):
                        return data["cwd"]
        except OSError:
            continue
    return project_dir.name


def _scan_roots(base_path: str, all_projects: bool = False) -> Tuple[List[dict], str]:
    """Build the list of root conversations the picker will show.

    Each entry: {root_uuid, project_dir, cwd, slug, title, first_seen,
                 last_active, node_count, native_in_subtree, native_at_root}.

    Returns (roots, scope_note).
    """
    base = CLAUDE_PROJECTS_DIR
    if not base.exists():
        return [], f"{base} does not exist"

    if all_projects:
        dirs = [d for d in base.iterdir() if d.is_dir() and list(d.glob("*.jsonl"))]
        scope = f"all of ~/.claude/projects/ ({len(dirs)} jsonl dirs)"
    else:
        target_slug = slug_for(base_path)
        target_dir = base / target_slug
        if target_dir.is_dir() and list(target_dir.glob("*.jsonl")):
            dirs = [target_dir]
            scope = f"{base_path}"
        else:
            return [], f"no Claude Code project at {base_path}"

    roots: List[dict] = []
    for d in dirs:
        by_uuid, session_leaf = load_project(d)
        if not by_uuid:
            continue
        nodes, native = build_nodes(by_uuid, session_leaf)
        real_cwd = _dir_real_cwd(d)
        for uuid_, n in nodes.items():
            if n["parent"] is not None:
                continue
            sub = _subtree(uuid_, nodes)
            # Pick the node with the latest timestamp as the "title" — it's the
            # most recently typed prompt in this conversation and is far more
            # useful to identify "where I left off" than the original root.
            latest_uuid = max(sub, key=lambda u: nodes[u]["timestamp"] or "")
            last_ts = nodes[latest_uuid]["timestamp"] or n["timestamp"] or ""
            roots.append({
                "root_uuid": uuid_,
                "project_dir": str(d),
                "cwd": real_cwd,
                "slug": d.name,
                "title": nodes[latest_uuid]["text"],
                "root_text": n["text"],
                "latest_uuid": latest_uuid,
                "first_seen": n["timestamp"] or "",
                "last_active": last_ts,
                "node_count": len(sub),
                "native_in_subtree": sum(1 for u in sub if u in native),
                "native_at_root": uuid_ in native,
            })

    roots.sort(key=lambda r: r["last_active"], reverse=True)
    return roots, scope


_PROJECT_PAGE = int(_cfg("page_size", default=15))


def _root_render(roots: List[dict], selected: int, offset: int,
                 scope_note: str = "", show_dir: bool = False) -> int:
    cols = shutil.get_terminal_size((140, 24)).columns
    visible = roots[offset:offset + _PROJECT_PAGE]
    num_w = max(2, len(str(len(roots))))
    max_n_w = max((len(str(r["node_count"])) for r in roots), default=1)
    # Pre-compute the relative-time column width across the visible page so the
    # node-count + title columns stay aligned even as the strings vary.
    ts_strs = [(_format_relative(r["last_active"]) if r["last_active"] else "—")
               for r in visible]
    ts_w = max((_vwidth(s) for s in ts_strs), default=8)
    nodes_w = max_n_w + len(" nodes")  # always pad to plural form for alignment
    head_w = 2 + 2 + (num_w + 1) + 1 + 1 + 1 + ts_w + 2 + nodes_w + 2
    dir_w = 18 if show_dir else 0
    title_w = max(15, cols - head_w - dir_w - 2)
    scope = _truncate_w(scope_note, cols - 9)  # "  scope: " is 9 cells
    lines = [
        bold("treeflow") + dim(" · pick a conversation root"),
        dim("    ↑↓ move · ⏎ select · 1-9 jump · a scope · q quit"),
        dim(f"    scope: {scope}"),
        "",
    ]
    for i, r in enumerate(visible):
        abs_idx = offset + i
        num_str = f"{abs_idx + 1:>{num_w}}."
        nat = purple("★") if r["native_at_root"] else " "
        ts_raw = ts_strs[i]
        ts_padded = ts_raw + " " * max(0, ts_w - _vwidth(ts_raw))
        nodes_label = "node " if r["node_count"] == 1 else "nodes"
        nodes_raw = f"{r['node_count']:>{max_n_w}} {nodes_label}"
        title = _truncate_w(re.sub(r"\s+", " ", r["title"]).strip(), title_w)
        dir_str = ""
        if show_dir:
            short = r["cwd"].rsplit("/", 1)[-1] or r["slug"]
            short = _truncate_w(short, dir_w)
            dir_str = dim(short) + " " * max(0, dir_w - _vwidth(short)) + "  "
        if abs_idx == selected:
            arrow = green("▶")
            row = (f"  {arrow} {bold(num_str)} {nat} {dim(ts_padded)}  "
                   f"{dim(nodes_raw)}  {dir_str}{bold(title)}")
        else:
            row = (f"    {dim(num_str)} {nat} {dim(ts_padded)}  "
                   f"{dim(nodes_raw)}  {dir_str}{title}")
        lines.append(row)
    if len(roots) > _PROJECT_PAGE:
        lines.append(dim(f"    … showing {offset + 1}-{offset + len(visible)} of {len(roots)}"))
    for line in lines:
        print(line)
    return len(lines)


def pick_project(default_path: str, all_projects: bool = False,
                 driver: Any = None) -> Optional[Tuple[str, str]]:
    """Interactive root-conversation picker.

    Returns (cwd, root_uuid) or None. `cwd` is the path to feed into the
    driver's gather() and `root_uuid` scopes subsequent commands to that
    root's subtree."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from termios import tcgetattr  # noqa: F401
    except ImportError:
        return None

    drv = driver or _get_driver(_cfg("tool", default="claude"))

    def setup(use_all: bool):
        roots, note = drv.scan_roots(default_path, all_projects=use_all)
        return roots, note

    use_all = all_projects
    roots, scope_note = setup(use_all)
    if not roots and not use_all:
        # No project at cwd — auto-toggle to all so the picker is still useful.
        use_all = True
        roots, scope_note = setup(True)
    if not roots:
        return None

    selected = 0
    offset = 0
    show_dir = use_all  # only show project-dir column when listing across dirs

    drawn = _root_render(roots, selected, offset, scope_note, show_dir)
    with _RawInput() as raw:
        while True:
            try:
                key = raw.read_key()
            except KeyboardInterrupt:
                _menu_clear(drawn)
                return None

            old_selected = selected
            if key == "down":
                selected = (selected + 1) % len(roots)
            elif key == "up":
                selected = (selected - 1) % len(roots)
            elif key == "enter":
                _menu_clear(drawn)
                r = roots[selected]
                return (r["cwd"], r["root_uuid"])
            elif key in ("q", "Q", "esc"):
                _menu_clear(drawn)
                return None
            elif key in ("a", "A"):
                use_all = not use_all
                roots, scope_note = setup(use_all)
                if not roots:
                    use_all = not use_all
                    roots, scope_note = setup(use_all)
                show_dir = use_all
                selected = 0
                offset = 0
                _menu_clear(drawn)
                drawn = _root_render(roots, selected, offset, scope_note, show_dir)
                continue
            elif key.isdigit() and key != "0":
                idx = int(key) - 1
                if 0 <= idx < len(roots):
                    _menu_clear(drawn)
                    r = roots[idx]
                    return (r["cwd"], r["root_uuid"])

            if selected != old_selected:
                if selected < offset:
                    offset = selected
                elif selected >= offset + _PROJECT_PAGE:
                    offset = selected - _PROJECT_PAGE + 1
                _menu_clear(drawn)
                drawn = _root_render(roots, selected, offset, scope_note, show_dir)


# (Old action menu removed — the post-project flow is now the unified
# `browse_project` browser with a persistent search box and Tab-cycled
# view modes. Use the `treeflow <subcommand>` CLI for non-interactive workflows.)


class _RawInput:
    """Hold the terminal in cbreak mode for the entire interactive session.
    Switching modes between every keystroke (as a per-call enter/exit did)
    lets the canonical-mode tty between reads buffer bytes the menu cares
    about, sometimes losing the leading ESC of an arrow sequence."""

    def __init__(self) -> None:
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.log = (os.environ.get("TREEFLOW_DEBUG_KEYS") == "1"
                    or bool(_cfg("debug", "log_keys", default=False)))

    def close(self) -> None:
        import termios
        # TCSAFLUSH (vs TCSANOW) drains any pending raw-mode input that wasn't
        # consumed before reverting to canonical mode — without it, stale bytes
        # (e.g. an arrow-key sequence the user typed-ahead) get fed to the next
        # input() call and show up literally as `^[[B`.
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)

    def __enter__(self) -> "_RawInput":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _getch(self) -> str:
        # Bypass Python's stdin TextIOWrapper buffering — go straight to the
        # underlying file descriptor. Without this, `sys.stdin.read(1)` can
        # block until a chunk fills even in cbreak mode, so users see "any key
        # closes the menu" because keystrokes only arrive once they hit Enter
        # (and then the buffered ESC sequence collapses into a single Enter).
        b = os.read(self.fd, 1)
        if not b:
            return ""  # EOF — caller treats as quit
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("utf-8", errors="replace")

    def _wait_more(self, timeout: float) -> bool:
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        return bool(r)

    def read_key(self) -> str:
        seq: List[str] = []

        def trace(label: str) -> str:
            if self.log:
                raw = "".join(seq)
                hexed = " ".join(f"{ord(c):02x}" for c in raw)
                sys.stderr.write(f"[treeflow-keys] {label}  raw=[{hexed}]\n")
                sys.stderr.flush()
            return label

        c = self._getch(); seq.append(c)
        if c == "":
            return trace("esc")  # stdin closed
        if c == "\t":
            return trace("tab")
        if c in ("\r", "\n"):
            return trace("enter")
        if c == "\x03":
            raise KeyboardInterrupt
        if c == "\x7f":
            return trace("backspace")
        if c != "\x1b":
            return trace(c)

        if not self._wait_more(0.15):
            return trace("esc")
        c2 = self._getch(); seq.append(c2)
        if c2 not in ("[", "O"):
            return trace("unknown")  # Alt+key or unrecognized — don't quit
        if not self._wait_more(0.15):
            return trace("esc")
        c3 = self._getch(); seq.append(c3)
        mapped = {"A": "up", "B": "down", "C": "right", "D": "left", "Z": "shift-tab"}.get(c3)
        if mapped:
            return trace(mapped)
        # CSI sequence with parameters (e.g. ESC [ 1 ; 5 A for Ctrl+Up). Read
        # bytes until we hit a final letter or "~", then report as unknown.
        while True:
            if c3 == "~" or c3.isalpha():
                break
            if not self._wait_more(0.05):
                break
            c3 = self._getch(); seq.append(c3)
        return trace("unknown")


def _read_key_via(raw: Optional[_RawInput] = None) -> str:
    """Convenience wrapper that uses an existing _RawInput if provided,
    otherwise creates a short-lived one (only used by the legacy code paths)."""
    if raw is not None:
        return raw.read_key()
    with _RawInput() as ri:
        return ri.read_key()


def _menu_clear(n: int) -> None:
    """Move cursor up n lines and clear each one."""
    for _ in range(n):
        sys.stdout.write("\x1b[1A\x1b[2K")
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    driver = _driver_for(args)

    if args.cmd:
        # Explicit subcommand — keep cwd as the default project unless -p was given.
        if args.path is None:
            args.path = os.getcwd()
        args.func(args)
        return

    # Non-tty interactive request: behave like `list` (legacy default).
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        chosen_path = args.path or os.getcwd()
        chosen_root = args.scope_root
        ns = argparse.Namespace(
            path=chosen_path, scope_root=chosen_root, cmd="list",
            tool=args.tool,
            leaves=False, limit=0, tree=False, json=False, func=cmd_browse,
        )
        ns.func(ns)
        return

    # Two-layer state machine: project picker → unified browser.
    # `BACK` from the browser pops back to the project picker; either layer's
    # `Ctrl+C` (KeyboardInterrupt) drops out of the whole interactive flow.
    layer = "project"
    chosen_path: Optional[str] = args.path
    chosen_root: Optional[str] = args.scope_root
    while True:
        if layer == "project":
            if chosen_path is not None:
                layer = "browse"
                continue
            picked = pick_project(os.getcwd(),
                                  all_projects=args.all_projects,
                                  driver=driver)
            if picked is None:
                return  # quit
            chosen_path, chosen_root = picked
            layer = "browse"
        elif layer == "browse":
            try:
                pd, nodes, native, raw = driver.gather(chosen_path, chosen_root)
            except SystemExit:
                return
            scope_summary = chosen_path
            if chosen_root:
                root_node = nodes.get(chosen_root)
                if root_node:
                    snippet = re.sub(r"\s+", " ", root_node["text"]).strip()
                    if len(snippet) > 50:
                        snippet = snippet[:49] + "…"
                    scope_summary = f"{chosen_path} · root {chosen_root[:8]} · {snippet}"
            result = browse_project(pd, nodes, native, scope_summary, driver=driver)
            if result is None:
                return
            if result is BACK:
                chosen_path = None
                chosen_root = None
                layer = "project"
                continue
            # result is a node dict — resume it via the driver. Auto-exec the
            # agent CLI if the user passed `-x` at the top level OR set
            # `auto_exec = true` under `[browser]` in their config.
            auto_exec = bool(getattr(args, "exec_", False)) or bool(
                _cfg("browser", "auto_exec", default=False))
            driver.emit_resume(pd, result, raw, exec_after=auto_exec)
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
