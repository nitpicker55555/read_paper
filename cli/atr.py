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


def _pick_node_render(items: List[dict], native_reachable: set, selected: int,
                      offset: int, project_dir: Path, leaf_count: int,
                      total: int, native_count: int, highlight: str = "",
                      query: str = "") -> int:
    cols = shutil.get_terminal_size((140, 24)).columns
    page = items[offset:offset + _NODE_PAGE]
    num_w = max(2, len(str(len(items))))
    # prefix cells: "  ▶ " + "NN. " + "MM-DD HH:MM" + "  " + "xxxxxxxx" + "  L  ★  "
    head_w = 4 + (num_w + 1) + 1 + 11 + 2 + 8 + 2 + 1 + 2 + 1 + 2
    text_w = max(15, cols - head_w - 2)

    proj_str = _truncate_w(str(project_dir), cols - 14)
    lines: List[str] = [
        dim(f"project dir:  {proj_str}"),
        (dim("nodes total: ") + bold(str(total))
         + dim("   leaves: ") + bold(str(leaf_count))
         + dim("   native: ") + purple(f"★ {native_count}")
         + dim("   atr-only: ") + str(total - native_count)),
    ]
    if query:
        lines.append(dim("query: ") + bold(repr(query))
                     + dim("   matches: ") + bold(str(len(items))))
    lines.append(dim("↑↓ move · ⏎ select · 1-9 jump · q quit"))
    lines.append("")
    head_line = (f"  {'#'.rjust(num_w + 1)}  {'time':<11}  {'uuid':<8}  L  ★  prompt")
    lines.append(dim(head_line))
    lines.append(dim("─" * min(cols - 1, head_w + min(text_w, 60))))

    for i, n in enumerate(page):
        abs_idx = offset + i
        num_str = f"{abs_idx + 1:>{num_w}}."
        ts = _format_time(n["timestamp"])
        uid = n["uuid"][:8]
        leaf = green("·") if not n["children"] else " "
        nat = purple("★") if n["uuid"] in native_reachable else " "
        text = re.sub(r"\s+", " ", n["text"]).strip()
        text = _truncate_w(text, text_w)
        text = _highlight(text, highlight)
        if abs_idx == selected:
            arrow = green("▶")
            row = (f"  {arrow} {bold(num_str)}  {dim(ts)}  {dim(uid)}  "
                   f"{leaf}  {nat}  {bold(text)}")
        else:
            row = (f"    {dim(num_str)}  {dim(ts)}  {dim(uid)}  "
                   f"{leaf}  {nat}  {text}")
        lines.append(row)

    if len(items) > _NODE_PAGE:
        lines.append(dim(
            f"    … showing {offset + 1}-{offset + len(page)} of {len(items)}"
        ))

    for line in lines:
        print(line)
    return len(lines)


def pick_node_interactive(items: List[dict], native_reachable: set,
                          project_dir: Path, leaf_count: int, total: int,
                          native_count: int, highlight: str = "",
                          query: str = "") -> Optional[dict]:
    """Paginated picker for node items. Returns the picked node or None.

    Matches the project picker's UX: ↑↓ navigate, Enter selects, 1-9 absolute
    jump within the full list, q / Esc cancels. Max 10 items per page with
    the rest reachable via arrow-key scrolling.
    """
    if not items or not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    selected = 0
    offset = 0
    drawn = _pick_node_render(items, native_reachable, selected, offset,
                              project_dir, leaf_count, total, native_count,
                              highlight=highlight, query=query)
    with _RawInput() as raw:
        while True:
            try:
                key = raw.read_key()
            except KeyboardInterrupt:
                _menu_clear(drawn)
                return None

            old_sel = selected
            if key == "down":
                selected = (selected + 1) % len(items)
            elif key == "up":
                selected = (selected - 1) % len(items)
            elif key == "enter":
                _menu_clear(drawn)
                return items[selected]
            elif key in ("q", "Q", "esc"):
                _menu_clear(drawn)
                return None
            elif key.isdigit() and key != "0":
                idx = int(key) - 1
                if 0 <= idx < len(items):
                    _menu_clear(drawn)
                    return items[idx]

            if selected != old_sel:
                if selected < offset:
                    offset = selected
                elif selected >= offset + _NODE_PAGE:
                    offset = selected - _NODE_PAGE + 1
                _menu_clear(drawn)
                drawn = _pick_node_render(items, native_reachable, selected,
                                          offset, project_dir, leaf_count,
                                          total, native_count,
                                          highlight=highlight, query=query)


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

    if args.pick and items and sys.stdin.isatty() and sys.stdout.isatty():
        # Picking needs the prompt to land right after the listing — skip pager.
        # User can still narrow with -n / search / leaves to fit screen.
        buf = io.StringIO()
        render(buf)
        sys.stdout.write(buf.getvalue())
    else:
        with_pager(render)


def _maybe_pick(args, items, nodes, native, pd, by_uuid,
                highlight: str = "", query: str = "") -> bool:
    """Returns True if the paginated picker was used (and the table dump should
    be skipped). Returns False if non-tty or non-pick mode — caller falls back
    to _show_items + no input prompt."""
    tree_mode = getattr(args, "tree", False)
    if not (args.pick and items and not tree_mode
            and sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    leaf_count = sum(1 for n in nodes.values() if not n["children"])
    choice = pick_node_interactive(
        items, native, pd,
        leaf_count=leaf_count, total=len(nodes), native_count=len(native),
        highlight=highlight, query=query,
    )
    if choice:
        emit_resume(pd, choice, by_uuid, exec_after=args.exec_)
    return True


def cmd_browse(args) -> None:
    pd, nodes, native, by_uuid = _gather(args.path, getattr(args, "scope_root", None))
    items = list(nodes.values())
    if args.leaves:
        items = [n for n in items if not n["children"]]
    items.sort(key=lambda n: n["timestamp"], reverse=True)
    if args.limit:
        items = items[: args.limit]

    if _maybe_pick(args, items, nodes, native, pd, by_uuid):
        return
    _show_items(args, items, nodes, native, pd)


def cmd_search(args) -> None:
    pd, nodes, native, by_uuid = _gather(args.path, getattr(args, "scope_root", None))
    q = args.query.lower()
    matches = [n for n in nodes.values() if q in n["text"].lower()]
    matches.sort(key=lambda n: n["timestamp"], reverse=True)
    if args.limit:
        matches = matches[: args.limit]

    if _maybe_pick(args, matches, nodes, native, pd, by_uuid,
                   highlight=args.query, query=args.query):
        return
    _show_items(args, matches, nodes, native, pd, highlight=args.query)


def cmd_tree(args) -> None:
    """Dedicated tree-view command. Always tree, never pick."""
    pd, nodes, native, _by_uuid = _gather(args.path, getattr(args, "scope_root", None))

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
        print(red(f"no uuid starts with {args.uuid!r}"), file=sys.stderr)
        sys.exit(2)
    if len(matches) > 1:
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
        print(yellow(f"⚠ {target[:8]} is not a user prompt ({by_uuid[target][0].get('type')}) — trying anyway"), file=sys.stderr)
    emit_resume(pd, node, by_uuid, exec_after=args.exec_)


def cmd_info(args) -> None:
    pd, nodes, native, by_uuid = _gather(args.path, getattr(args, "scope_root", None))
    matches = [u for u in nodes if u.startswith(args.uuid)]
    if not matches:
        print(red(f"no uuid starts with {args.uuid!r}"), file=sys.stderr)
        sys.exit(2)
    if len(matches) > 1:
        print(red(f"prefix {args.uuid!r} is ambiguous"), file=sys.stderr)
        for m in matches[:6]:
            print(f"  {m}", file=sys.stderr)
        sys.exit(2)
    n = nodes[matches[0]]
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


# ---------- arg parsing ----------

def _add_common_pick(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-pick", action="store_false", dest="pick",
                   help="don't prompt for pick after listing")
    p.add_argument("-x", "--exec", action="store_true", dest="exec_",
                   help="after picking, exec `claude --resume <new-sid>` directly")


def _add_common_view(p: argparse.ArgumentParser, default_limit: int = 0) -> None:
    p.add_argument("-n", "--limit", type=int, default=default_limit,
                   help="max results (default 0 = no limit, scrollable)")
    p.add_argument("-t", "--tree", action="store_true",
                   help="render as ASCII tree instead of table (overrides pick)")


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
                    help="in the interactive picker, include roots from every "
                         "~/.claude/projects/ dir, not just the one matching cwd")
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", aliases=["ls"], help="list conversations, most recent first")
    p_list.add_argument("-L", "--leaves", action="store_true", help="only branch tails")
    _add_common_view(p_list)
    _add_common_pick(p_list)
    p_list.set_defaults(func=cmd_browse, pick=True, exec_=False, leaves=False, tree=False)

    p_leaves = sub.add_parser("leaves", help="show only leaf nodes (branch tails)")
    _add_common_view(p_leaves)
    _add_common_pick(p_leaves)
    p_leaves.set_defaults(func=cmd_browse, pick=True, exec_=False, leaves=True, tree=False)

    p_search = sub.add_parser("search", aliases=["s"], help="search by keyword in prompt content")
    p_search.add_argument("query")
    _add_common_view(p_search)
    _add_common_pick(p_search)
    p_search.set_defaults(func=cmd_search, pick=True, exec_=False, tree=False)

    p_tree = sub.add_parser("tree", aliases=["t"], help="ASCII tree of the whole project")
    p_tree.add_argument("-m", "--match", default="",
                        help="highlight prompts matching this substring")
    p_tree.set_defaults(func=cmd_tree)

    p_resume = sub.add_parser("resume", aliases=["r"], help="generate resume command for a uuid prefix")
    p_resume.add_argument("uuid")
    p_resume.add_argument("-x", "--exec", action="store_true", dest="exec_")
    p_resume.set_defaults(func=cmd_resume)

    p_info = sub.add_parser("info", aliases=["i"], help="show full prompt for a node")
    p_info.add_argument("uuid")
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


# ---------- interactive menu (tab navigation) ----------

# Menu entries: (cmd, label, hint, takes_input_label)
_MENU = [
    ("list",   "list",   "every node, table view, most recent first",  None),
    ("leaves", "leaves", "only branch tails (leaf nodes)",              None),
    ("tree",   "tree",   "compact ASCII tree (linear runs collapsed)",  None),
    ("search", "search", "find prompts by keyword",                     "search query"),
    ("resume", "resume", "generate resume command for a uuid prefix",   "uuid prefix"),
    ("info",   "info",   "show full prompt for a node",                 "uuid prefix"),
    ("quit",   "quit",   "exit",                                        None),
]


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


def _menu_render(selected: int, project_dir: Path, scope_summary: str = "") -> int:
    """Render menu lines (header + options). Returns number of lines drawn."""
    cols = shutil.get_terminal_size((140, 24)).columns
    proj_line = _truncate_w(str(project_dir), cols - 13)  # "  project:  " = 12 cells
    lines = [
        bold("atr") + dim(" · pick an action"),
        dim("    ↑↓ move · ⏎ select · 1-7 jump · q quit"),
        dim(f"    project: {proj_line}"),
    ]
    if scope_summary:
        lines.append(dim(f"    root:    {_truncate_w(scope_summary, cols - 13)}"))
    lines.append("")
    for i, (_cmd, label, hint, _inp) in enumerate(_MENU, 1):
        if i - 1 == selected:
            arrow = green("▶")
            row = f"  {arrow} {bold(str(i) + '. ' + label):<14}  {dim(hint)}"
        else:
            row = f"    {dim(str(i) + '. ')}{label:<10}  {dim(hint)}"
        lines.append(row)
    for line in lines:
        print(line)
    return len(lines)


def _menu_clear(n: int) -> None:
    """Move cursor up n lines and clear each one."""
    for _ in range(n):
        sys.stdout.write("\x1b[1A\x1b[2K")
    sys.stdout.flush()


def interactive_menu(path: str, scope_root: Optional[str] = None) -> Optional[Tuple[str, Optional[str]]]:
    """Tab-driven menu. Returns (cmd, input_text_or_None) or None to quit.
    Falls back to None on non-tty environments."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    pd = project_dir_for(path)
    selected = 0

    try:
        from termios import tcgetattr  # smoke-test termios is importable
    except ImportError:
        return None  # non-Unix; caller falls back to default `list`

    # When the user picked a specific root, summarize it in the header so
    # they remember what subtree the next action will operate on.
    scope_summary = ""
    if scope_root:
        try:
            by_uuid, session_leaf = load_project(pd)
            nodes, _native = build_nodes(by_uuid, session_leaf)
            if scope_root in nodes:
                first_line = re.sub(r"\s+", " ", nodes[scope_root]["text"]).strip()
                if len(first_line) > 60:
                    first_line = first_line[:59] + "…"
                size = len(_subtree(scope_root, nodes))
                scope_summary = f"{scope_root[:8]}  {size}n  {first_line}"
        except Exception:  # noqa: BLE001 -- header is best-effort
            scope_summary = scope_root[:8]

    drawn = _menu_render(selected, pd, scope_summary)
    # Run the navigation loop entirely inside cbreak, capture the chosen
    # command, then exit the with block BEFORE prompting for any text input.
    # input()'s readline does its own termios handling — overlapping that with
    # our raw-mode context leaves stdin in a half-canonical half-cbreak limbo
    # where backspace/arrow keys at the input prompt produce raw byte echoes
    # (`^[[B`, `^?`) instead of editing the line.
    chosen_cmd: Optional[str] = None
    chosen_prompt_label: Optional[str] = None
    with _RawInput() as raw:
        while True:
            try:
                key = raw.read_key()
            except KeyboardInterrupt:
                _menu_clear(drawn)
                print(dim("(cancelled)"))
                return None

            new_sel = selected
            if key == "down":
                new_sel = (selected + 1) % len(_MENU)
            elif key == "up":
                new_sel = (selected - 1) % len(_MENU)
            elif key == "enter":
                cmd, _label, _hint, prompt_label = _MENU[selected]
                _menu_clear(drawn)
                chosen_cmd = cmd
                chosen_prompt_label = prompt_label
                break
            elif key in ("q", "Q", "esc"):
                _menu_clear(drawn)
                return None
            elif key.isdigit() and key != "0":
                idx = int(key) - 1
                if 0 <= idx < len(_MENU):
                    cmd, _label, _hint, prompt_label = _MENU[idx]
                    _menu_clear(drawn)
                    chosen_cmd = cmd
                    chosen_prompt_label = prompt_label
                    break

            if new_sel != selected:
                selected = new_sel
                _menu_clear(drawn)
                drawn = _menu_render(selected, pd, scope_summary)

    # Terminal is back in canonical mode here (with stdin flushed by TCSAFLUSH).
    if chosen_cmd is None or chosen_cmd == "quit":
        return None
    if chosen_prompt_label:
        try:
            val = input(bold(chosen_prompt_label + ": ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not val:
            return None
        return (chosen_cmd, val)
    return (chosen_cmd, None)


def _ns_for(cmd: str, value: Optional[str], path: str,
            scope_root: Optional[str] = None) -> argparse.Namespace:
    """Build an argparse Namespace mimicking the chosen subcommand's defaults."""
    base = {"path": path, "scope_root": scope_root}
    if cmd == "list":
        return argparse.Namespace(**base, cmd="list", leaves=False, limit=0,
                                  tree=False, pick=True, exec_=False, func=cmd_browse)
    if cmd == "leaves":
        return argparse.Namespace(**base, cmd="leaves", leaves=True, limit=0,
                                  tree=False, pick=True, exec_=False, func=cmd_browse)
    if cmd == "tree":
        return argparse.Namespace(**base, cmd="tree", match="", func=cmd_tree)
    if cmd == "search":
        return argparse.Namespace(**base, cmd="search", query=value or "",
                                  limit=0, tree=False, pick=True, exec_=False,
                                  func=cmd_search)
    if cmd == "resume":
        return argparse.Namespace(**base, cmd="resume", uuid=value or "",
                                  exec_=False, func=cmd_resume)
    if cmd == "info":
        return argparse.Namespace(**base, cmd="info", uuid=value or "",
                                  func=cmd_info)
    raise ValueError(f"unknown menu cmd: {cmd}")


def main(argv: Optional[List[str]] = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cmd:
        # Explicit subcommand — keep cwd as the default project unless -p was given.
        if args.path is None:
            args.path = os.getcwd()
        args.func(args)
        return

    # Interactive flow: first pick a root conversation, then pick an action.
    chosen_path = args.path
    chosen_root = args.scope_root
    if chosen_path is None and sys.stdin.isatty() and sys.stdout.isatty():
        picked = pick_project(os.getcwd(), all_projects=args.all_projects)
        if picked is None:
            return  # user quit
        chosen_path, chosen_root = picked
    elif chosen_path is None:
        chosen_path = os.getcwd()

    choice = interactive_menu(chosen_path, chosen_root) if (sys.stdin.isatty() and sys.stdout.isatty()) else None
    if choice is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            args = argparse.Namespace(
                path=chosen_path, scope_root=chosen_root, cmd="list",
                leaves=False, limit=0, tree=False, pick=False, exec_=False,
                func=cmd_browse,
            )
            args.func(args)
        return
    cmd, value = choice
    args = _ns_for(cmd, value, chosen_path, scope_root=chosen_root)
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
