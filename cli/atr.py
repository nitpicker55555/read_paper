#!/usr/bin/env python3
"""agent tree resume — browse and resume any node in your local Claude Code
conversation tree, including abandoned sibling branches that `claude --resume`
can't reach natively.

Quick usage:
    atr                       interactive list of conversations in current dir
    atr -L                    only branch tails (leaf nodes)
    atr "browser tools"       keyword search across prompts
    atr resume <uuid-prefix>  generate resume command for a specific node
    atr -x                    after picking, exec `claude --resume` for you
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
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)


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
        }
    for u, n in nodes.items():
        if n["parent"] and n["parent"] in nodes:
            nodes[n["parent"]]["children"].append(u)

    # native_reachable: set of user-prompt uuids that --resume <sid> physically lands on
    native_reachable: set = set()
    for _sid, leaf in session_leaf.items():
        cur = leaf
        seen: set = set()
        while cur and cur not in seen:
            seen.add(cur)
            m = by_uuid.get(cur)
            if not m:
                break
            if _is_user_prompt(m[0]):
                native_reachable.add(cur)
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


# ---------- listing / display ----------

def _format_time(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return ts[:16]


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


_NODE_PAGE = 10
_VIEW_CYCLE = ("tree", "list", "leaves")


def _flatten_tree(roots: List[str], nodes: Dict[str, dict]) -> List[Tuple[str, str, str, bool]]:
    """Iterative depth-first walk that mirrors render_tree's compact collapsing.

    Yields (uuid_or_None, prefix, branch_glyph, is_last). When uuid is None the
    entry is the "⋮ N hidden" filler — not selectable. Linear single-child runs
    are collapsed so a 1700-node tree renders in a screenful of structural rows.
    """
    out: List[Tuple[Optional[str], str, str, bool]] = []

    def sort_key(uid: str) -> str:
        return nodes[uid]["timestamp"] or ""

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
                out.append((tail, cont_prefix, "└─ ", True))
            else:
                out.append((uid, prefix, glyph, is_last))
                out.append((None, cont_prefix, f"⋮ ({run_len - 2} hidden)", True))
                out.append((tail, cont_prefix, "└─ ", True))

            tail_children = sorted(nodes[tail]["children"], key=sort_key)
            child_prefix = cont_prefix + ("   " if run_len > 1 else "")
            n_kids = len(tail_children)
            for i in range(n_kids - 1, -1, -1):
                child_last = (i == n_kids - 1)
                child_glyph = "└─ " if child_last else "├─ "
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
        bold("atr") + dim("  ") + dim(_truncate_w(scope_summary, cols - 6)),
        dim(f"  {proj_str}"),
        (dim("  nodes ") + bold(str(len(all_nodes)))
         + dim(" · leaves ") + bold(str(leaf_total))
         + dim(" · native ") + purple(f"★ {len(native)}")
         + dim(" · atr-only ") + str(len(all_nodes) - len(native))),
    ]
    # search bar — always visible, cursor block at end
    cursor = green("▌")
    search_show = search if search else dim("(type to filter)")
    lines.append("")
    lines.append(f"  {bold('search:')} {search_show}{cursor}  "
                 + dim(f" ({len(items)} match)" if search else f" ({len(items)} total)"))
    # view tabs
    tabs = []
    for v in _VIEW_CYCLE:
        if v == view:
            tabs.append(green("[") + bold(v) + green("]"))
        else:
            tabs.append(dim(f" {v} "))
    lines.append("  " + "  ".join(tabs) + dim("   (⇥ next view)"))
    lines.append(dim("  ↑↓ move · ⏎ resume · ⌫ erase · ⎋ back · ^C quit"))
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

    if view == "tree":
        roots = [u for u, n in all_nodes.items()
                 if not (n["parent"] and n["parent"] in all_nodes)]
        flat = _flatten_tree(roots, all_nodes)
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
            # In tree view, keep structural parents so the matching nodes still
            # show in context. Simple heuristic: include any line whose node
            # text matches (filler entries are removed when searching).
            items = [it for it in items
                     if (not it["filler"]) and search in (it["node"]["text"] or "").lower()]
        return items

    if view == "leaves":
        nodes_list = [n for n in all_nodes.values() if not n["children"]]
    else:  # list
        nodes_list = list(all_nodes.values())
    nodes_list.sort(key=lambda n: n["timestamp"] or "", reverse=True)
    if search:
        nodes_list = [n for n in nodes_list if search in (n["text"] or "").lower()]
    return nodes_list


def browse_project(project_dir: Path, all_nodes: Dict[str, dict], native: set,
                   scope_summary: str = "") -> Optional[object]:
    """The unified post-project browser. Combines view-mode switching with a
    live search box. Returns either a node dict (to resume), BACK (go back to
    project picker), or None (quit)."""
    if not all_nodes or not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    state = {
        "view": "tree",
        "search": "",
        "selected": 0,
        "offset": 0,
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
                    if node:
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
        + dim(f"   atr-only: ") + str(total - native)
    )


def _gather(path: str, scope_root: Optional[str] = None
            ) -> Tuple[Path, Dict[str, dict], set, Dict[str, Tuple[dict, str]]]:
    pd = project_dir_for(path)
    if not pd.exists():
        print(red(f"Claude Code project directory not found: {pd}"), file=sys.stderr)
        print(dim(f"  (slug = {slug_for(path)})"), file=sys.stderr)
        sys.exit(2)
    by_uuid, session_leaf = load_project(pd)
    nodes, native = build_nodes(by_uuid, session_leaf)
    if scope_root:
        # Allow uuid prefix match for the CLI -r flag.
        matches = [u for u in nodes if u.startswith(scope_root)]
        if not matches:
            print(red(f"scope root uuid {scope_root!r} not found in this project"),
                  file=sys.stderr)
            sys.exit(2)
        if len(matches) > 1:
            print(red(f"scope root prefix {scope_root!r} is ambiguous"), file=sys.stderr)
            sys.exit(2)
        keep = _subtree(matches[0], nodes)
        nodes = {u: nodes[u] for u in keep}
        native = native & keep
    return pd, nodes, native, by_uuid


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
            + dim("   atr-only: ") + str(len(nodes) - len(native)),
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
    """CLI dump for `atr list` / `atr leaves`. Pure print, no interactive
    picker — the picker is reached via bare `atr` which opens browse_project."""
    pd, nodes, native, _by_uuid = _gather(args.path, getattr(args, "scope_root", None))
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
    pd, nodes, native, _by_uuid = _gather(args.path, getattr(args, "scope_root", None))
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
    pd, nodes, native, _by_uuid = _gather(args.path, getattr(args, "scope_root", None))

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
            + dim("   atr-only: ") + str(len(nodes) - len(native)),
            out,
        )
        _emit("", out)
        roots = [u for u, n in nodes.items() if not (n["parent"] and n["parent"] in nodes)]
        render_tree(roots, nodes, native, out, highlight=args.match or "")

    with_pager(render)


def cmd_resume(args) -> None:
    pd, nodes, _native, by_uuid = _gather(args.path, getattr(args, "scope_root", None))
    matches = [u for u in by_uuid if u.startswith(args.uuid)]
    if not matches:
        _fail(f"no uuid starts with {args.uuid!r}", args)
    if len(matches) > 1:
        if getattr(args, "json", False):
            _emit_json({
                "error": "ambiguous_prefix",
                "prefix": args.uuid,
                "matches": [
                    {
                        "uuid": m,
                        "text": _user_text((by_uuid[m][0].get("message") or {}).get("content"))[:80],
                    }
                    for m in matches[:20]
                ],
            })
            sys.exit(2)
        print(red(f"prefix {args.uuid!r} is ambiguous ({len(matches)} matches):"), file=sys.stderr)
        for m in matches[:6]:
            text = _user_text((by_uuid[m][0].get("message") or {}).get("content"))[:60]
            print(f"  {m}  {dim(text)}", file=sys.stderr)
        sys.exit(2)
    target = matches[0]
    node = nodes.get(target)
    if node is None:
        text = _user_text((by_uuid[target][0].get("message") or {}).get("content"))[:80]
        node = {"uuid": target, "text": text}
        if not getattr(args, "json", False):
            print(yellow(f"⚠ {target[:8]} is not a user prompt ({by_uuid[target][0].get('type')}) — trying anyway"), file=sys.stderr)

    new_sid, chain_len, new_path = write_synthetic_session(pd, target, by_uuid)
    cmd_str = f"claude --resume {new_sid}"
    if getattr(args, "json", False):
        _emit_json({
            "target_uuid": target,
            "target_text": node.get("text", ""),
            "session_id": new_sid,
            "file": str(new_path),
            "chain_length": chain_len,
            "command": cmd_str,
        })
    else:
        print()
        print(green(f"  ✓ synthesized session ({chain_len}-link chain) → {new_path.name}"))
        print(bold(f"  $ {cmd_str}"))
        print()
    if args.exec_:
        if not getattr(args, "json", False):
            print(dim("  → exec…"))
        try:
            os.execvp("claude", ["claude", "--resume", new_sid])
        except FileNotFoundError:
            print(red("  ! `claude` not found — install Claude Code CLI and put it on PATH"))
            sys.exit(2)


def cmd_info(args) -> None:
    pd, nodes, native, by_uuid = _gather(args.path, getattr(args, "scope_root", None))
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
    `--all` to scan every jsonl dir under ~/.claude/projects/."""
    roots, scope = _scan_roots(args.path or os.getcwd(),
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
                    "text": r["title"],
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
        ts = "—"
        if r["last_active"]:
            try:
                ts = datetime.fromisoformat(r["last_active"].replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
            except ValueError:
                ts = r["last_active"][:11]
        title = re.sub(r"\s+", " ", r["title"]).strip()
        nodes_str = f"{r['node_count']:>4}n"
        print(f"  {i:>3}. {nat}{dim(r['root_uuid'][:8])}  {dim(ts)}  "
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
            "atr_only": len(nodes) - len(native),
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
        prog="atr",
        description="agent tree resume — browse and resume any node in your local "
                    "Claude Code conversation tree, including abandoned branches.",
    )
    ap.add_argument("-p", "--path", default=None,
                    help="project path (default: pick interactively or use cwd)")
    ap.add_argument("-r", "--root", default=None, dest="scope_root",
                    help="scope to a specific root conversation (uuid prefix)")
    ap.add_argument("-a", "--all", action="store_true", dest="all_projects",
                    help="include roots from every ~/.claude/projects/ dir, "
                         "not just the one matching cwd")
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
    p_resume.add_argument("-x", "--exec", action="store_true", dest="exec_")
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
            timestamps = [nodes[u]["timestamp"] for u in sub if nodes[u]["timestamp"]]
            last_ts = max(timestamps, default=n["timestamp"] or "")
            roots.append({
                "root_uuid": uuid_,
                "project_dir": str(d),
                "cwd": real_cwd,
                "slug": d.name,
                "title": n["text"],
                "first_seen": n["timestamp"] or "",
                "last_active": last_ts,
                "node_count": len(sub),
                "native_in_subtree": sum(1 for u in sub if u in native),
                "native_at_root": uuid_ in native,
            })

    roots.sort(key=lambda r: r["last_active"], reverse=True)
    return roots, scope


_PROJECT_PAGE = 12


def _root_render(roots: List[dict], selected: int, offset: int,
                 scope_note: str = "", show_dir: bool = False) -> int:
    cols = shutil.get_terminal_size((140, 24)).columns
    visible = roots[offset:offset + _PROJECT_PAGE]
    num_w = max(2, len(str(len(roots))))
    max_n_w = max((len(str(r["node_count"])) for r in roots), default=1) + 1
    # fixed prefix width (display cells): "  ▶ NN. ★ MM-DD HH:MM  NNNn  "
    head_w = 2 + 2 + (num_w + 1) + 1 + 1 + 1 + 11 + 2 + max_n_w + 2
    dir_w = 18 if show_dir else 0
    title_w = max(15, cols - head_w - dir_w - 2)
    scope = _truncate_w(scope_note, cols - 9)  # "  scope: " is 9 cells
    lines = [
        bold("atr") + dim(" · pick a conversation root"),
        dim("    ↑↓ move · ⏎ select · 1-9 jump · a scope · q quit"),
        dim(f"    scope: {scope}"),
        "",
    ]
    for i, r in enumerate(visible):
        abs_idx = offset + i
        num_str = f"{abs_idx + 1:>{num_w}}."
        nat = purple("★") if r["native_at_root"] else " "
        ts = "—"
        if r["last_active"]:
            try:
                ts = datetime.fromisoformat(r["last_active"].replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
            except ValueError:
                ts = r["last_active"][:11]
        nodes_str = f"{r['node_count']:>{max_n_w - 1}}n"
        title = _truncate_w(re.sub(r"\s+", " ", r["title"]).strip(), title_w)
        dir_str = ""
        if show_dir:
            short = r["cwd"].rsplit("/", 1)[-1] or r["slug"]
            short = _truncate_w(short, dir_w)
            dir_str = dim(short) + " " * max(0, dir_w - _vwidth(short)) + "  "
        if abs_idx == selected:
            arrow = green("▶")
            row = (f"  {arrow} {bold(num_str)} {nat} {dim(ts)}  {dim(nodes_str)}  "
                   f"{dir_str}{bold(title)}")
        else:
            row = (f"    {dim(num_str)} {nat} {dim(ts)}  {dim(nodes_str)}  "
                   f"{dir_str}{title}")
        lines.append(row)
    if len(roots) > _PROJECT_PAGE:
        lines.append(dim(f"    … showing {offset + 1}-{offset + len(visible)} of {len(roots)}"))
    for line in lines:
        print(line)
    return len(lines)


def pick_project(default_path: str, all_projects: bool = False) -> Optional[Tuple[str, str]]:
    """Interactive root-conversation picker.

    Returns (cwd, root_uuid) or None. `cwd` is the path to feed into
    project_dir_for() and `root_uuid` scopes subsequent commands to that root's
    subtree."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from termios import tcgetattr  # noqa: F401
    except ImportError:
        return None

    def setup(use_all: bool):
        roots, note = _scan_roots(default_path, all_projects=use_all)
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
# view modes. Use the `atr <subcommand>` CLI for non-interactive workflows.)


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
        self.log = os.environ.get("ATR_DEBUG_KEYS") == "1"

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
                sys.stderr.write(f"[atr-keys] {label}  raw=[{hexed}]\n")
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
            picked = pick_project(os.getcwd(), all_projects=args.all_projects)
            if picked is None:
                return  # quit
            chosen_path, chosen_root = picked
            layer = "browse"
        elif layer == "browse":
            pd = project_dir_for(chosen_path)
            if not pd.exists():
                print(red(f"Claude Code project directory not found: {pd}"),
                      file=sys.stderr)
                return
            by_uuid, session_leaf = load_project(pd)
            nodes, native = build_nodes(by_uuid, session_leaf)
            if chosen_root:
                keep = _subtree(chosen_root, nodes)
                nodes = {u: nodes[u] for u in keep}
                native = native & keep
            scope_summary = chosen_path
            if chosen_root:
                root_node = nodes.get(chosen_root)
                if root_node:
                    snippet = re.sub(r"\s+", " ", root_node["text"]).strip()
                    if len(snippet) > 50:
                        snippet = snippet[:49] + "…"
                    scope_summary = f"{chosen_path} · root {chosen_root[:8]} · {snippet}"
            result = browse_project(pd, nodes, native, scope_summary)
            if result is None:
                return
            if result is BACK:
                chosen_path = None
                chosen_root = None
                layer = "project"
                continue
            # result is a node dict — resume it
            emit_resume(pd, result, by_uuid, exec_after=False)
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
