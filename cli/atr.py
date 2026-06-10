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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def pick_interactive(items: List[dict]) -> Optional[dict]:
    if not items:
        return None
    if not sys.stdin.isatty():
        return None
    try:
        s = input(bold("pick number to generate resume (enter to skip): ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not s:
        return None
    try:
        i = int(s) - 1
        if 0 <= i < len(items):
            return items[i]
    except ValueError:
        pass
    print(red("invalid number"), file=sys.stderr)
    return None


# ---------- commands ----------

def _print_summary(project_dir: Path, total: int, native: int, leaves: int) -> None:
    print(dim(f"project dir: {project_dir}"))
    print(
        dim("nodes total: ") + bold(str(total))
        + dim("   leaves: ") + bold(str(leaves))
        + dim("   native: ") + purple(f"★ {native}")
        + dim(f"   atr-only: ") + str(total - native)
    )


def _gather(path: str) -> Tuple[Path, Dict[str, dict], set, Dict[str, Tuple[dict, str]]]:
    pd = project_dir_for(path)
    if not pd.exists():
        print(red(f"Claude Code project directory not found: {pd}"), file=sys.stderr)
        print(dim(f"  (slug = {slug_for(path)})"), file=sys.stderr)
        sys.exit(2)
    by_uuid, session_leaf = load_project(pd)
    nodes, native = build_nodes(by_uuid, session_leaf)
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


def cmd_browse(args) -> None:
    pd, nodes, native, by_uuid = _gather(args.path)
    items = list(nodes.values())
    if args.leaves:
        items = [n for n in items if not n["children"]]
    items.sort(key=lambda n: n["timestamp"], reverse=True)
    if args.limit:
        items = items[: args.limit]

    _show_items(args, items, nodes, native, pd)

    if args.pick and not getattr(args, "tree", False):
        choice = pick_interactive(items)
        if choice:
            emit_resume(pd, choice, by_uuid, exec_after=args.exec_)


def cmd_search(args) -> None:
    pd, nodes, native, by_uuid = _gather(args.path)
    q = args.query.lower()
    matches = [n for n in nodes.values() if q in n["text"].lower()]
    matches.sort(key=lambda n: n["timestamp"], reverse=True)
    if args.limit:
        matches = matches[: args.limit]

    _show_items(args, matches, nodes, native, pd, highlight=args.query)

    if args.pick and not getattr(args, "tree", False):
        choice = pick_interactive(matches)
        if choice:
            emit_resume(pd, choice, by_uuid, exec_after=args.exec_)


def cmd_tree(args) -> None:
    """Dedicated tree-view command. Always tree, never pick."""
    pd, nodes, native, _by_uuid = _gather(args.path)

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
    pd, nodes, _native, by_uuid = _gather(args.path)
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
    pd, nodes, native, by_uuid = _gather(args.path)
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


# ---------- project picker ----------

def _scan_projects() -> List[dict]:
    """Walk CLAUDE_PROJECTS_DIR and return {slug, path, sessions, mtime}
    entries, sorted by latest jsonl mtime (most recently active first)."""
    base = CLAUDE_PROJECTS_DIR
    if not base.exists():
        return []
    projects: List[dict] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        jsonls = list(d.glob("*.jsonl"))
        if not jsonls:
            continue
        try:
            latest_mtime = max(j.stat().st_mtime for j in jsonls)
        except OSError:
            continue
        # Recover the real project path by reading any message's `cwd` field — the
        # slug rule (slashes + underscores both → '-') is lossy so we can't unslug.
        real_path = d.name
        for j in sorted(jsonls, key=lambda x: x.stat().st_mtime, reverse=True):
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
                            real_path = data["cwd"]
                            break
                    if real_path != d.name:
                        break
            except OSError:
                continue
        projects.append({
            "slug": d.name,
            "path": real_path,
            "sessions": len(jsonls),
            "mtime": latest_mtime,
        })
    projects.sort(key=lambda p: p["mtime"], reverse=True)
    return projects


_PROJECT_PAGE = 12


def _project_render(projects: List[dict], selected: int, current_idx: int,
                    offset: int) -> int:
    cols = shutil.get_terminal_size((120, 24)).columns
    visible = projects[offset:offset + _PROJECT_PAGE]
    num_w = max(2, len(str(len(projects))))
    label_w = max(20, cols - 38 - num_w)
    lines = [
        bold("atr") + dim("  pick a project  ")
        + dim("(↑/↓ move · Enter select · 1-9 jump · q quit)"),
        dim(f"  base: {CLAUDE_PROJECTS_DIR}"),
        "",
    ]
    for i, p in enumerate(visible):
        absolute_idx = offset + i
        # Absolute 1-based label — stays attached to the project as you scroll
        # so the user can always see "this is item 13 of 38". Single-digit
        # shortcuts still map to the absolute positions 1-9.
        num_str = f"{absolute_idx + 1:>{num_w}}."
        marker = green("●") if absolute_idx == current_idx else " "
        mtime_s = datetime.fromtimestamp(p["mtime"]).strftime("%m-%d %H:%M")
        sess = f"{p['sessions']:>3} sess"
        label = p["path"]
        if len(label) > label_w:
            label = "…" + label[-(label_w - 1):]
        if absolute_idx == selected:
            arrow = green("▶")
            row = (f"  {arrow} {bold(num_str)} {marker} {bold(label):<{label_w}}  "
                   f"{dim(sess)}  {dim(mtime_s)}")
        else:
            row = (f"    {dim(num_str)} {marker} {label:<{label_w}}  "
                   f"{dim(sess)}  {dim(mtime_s)}")
        lines.append(row)
    if len(projects) > _PROJECT_PAGE:
        lines.append(dim(f"    … showing {offset + 1}-{offset + len(visible)} of {len(projects)}"))
    for line in lines:
        print(line)
    return len(lines)


def pick_project(default_path: str) -> Optional[str]:
    """Interactive project picker. Returns selected path or None."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from termios import tcgetattr  # noqa: F401  -- termios import check
    except ImportError:
        return None
    projects = _scan_projects()
    if not projects:
        return None

    cwd_slug = slug_for(default_path)
    current_idx = next((i for i, p in enumerate(projects) if p["slug"] == cwd_slug), -1)
    selected = current_idx if current_idx >= 0 else 0
    offset = max(0, min(selected - _PROJECT_PAGE // 2, len(projects) - _PROJECT_PAGE))

    drawn = _project_render(projects, selected, current_idx, offset)
    with _RawInput() as raw:
        while True:
            try:
                key = raw.read_key()
            except KeyboardInterrupt:
                _menu_clear(drawn)
                return None

            old_selected = selected
            if key == "down":
                selected = (selected + 1) % len(projects)
            elif key == "up":
                selected = (selected - 1) % len(projects)
            elif key == "enter":
                _menu_clear(drawn)
                return projects[selected]["path"]
            elif key in ("q", "Q", "esc"):
                _menu_clear(drawn)
                return None
            elif key.isdigit() and key != "0":
                # 1-9 always jump to absolute positions 1-9 (top 9 most recent).
                # For project #10+, the user navigates with arrow keys.
                idx = int(key) - 1
                if 0 <= idx < len(projects):
                    _menu_clear(drawn)
                    return projects[idx]["path"]

            if selected != old_selected:
                if selected < offset:
                    offset = selected
                elif selected >= offset + _PROJECT_PAGE:
                    offset = selected - _PROJECT_PAGE + 1
                _menu_clear(drawn)
                drawn = _project_render(projects, selected, current_idx, offset)


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
        termios.tcsetattr(self.fd, termios.TCSANOW, self.old)

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


def _menu_render(selected: int, project_dir: Path) -> int:
    """Render menu lines (header + options). Returns number of lines drawn."""
    lines = [
        bold("atr") + dim("  pick an action  ") + dim("(↑/↓ move · Enter select · 1-7 jump · q quit)"),
        dim(f"  project: {project_dir}"),
        "",
    ]
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


def interactive_menu(path: str) -> Optional[Tuple[str, Optional[str]]]:
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

    drawn = _menu_render(selected, pd)
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
                raw.close()  # drop cbreak before regular input()
                if cmd == "quit":
                    return None
                if prompt_label:
                    try:
                        val = input(bold(prompt_label + ": ")).strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return None
                    if not val:
                        return None
                    return (cmd, val)
                return (cmd, None)
            elif key in ("q", "Q", "esc"):
                _menu_clear(drawn)
                return None
            elif key.isdigit() and key != "0":
                idx = int(key) - 1
                if 0 <= idx < len(_MENU):
                    cmd, _label, _hint, prompt_label = _MENU[idx]
                    _menu_clear(drawn)
                    raw.close()
                    if cmd == "quit":
                        return None
                    if prompt_label:
                        try:
                            val = input(bold(prompt_label + ": ")).strip()
                        except (EOFError, KeyboardInterrupt):
                            print()
                            return None
                        if not val:
                            return None
                        return (cmd, val)
                    return (cmd, None)

            if new_sel != selected:
                selected = new_sel
                _menu_clear(drawn)
                drawn = _menu_render(selected, pd)


def _ns_for(cmd: str, value: Optional[str], path: str) -> argparse.Namespace:
    """Build an argparse Namespace mimicking the chosen subcommand's defaults."""
    if cmd == "list":
        return argparse.Namespace(path=path, cmd="list", leaves=False, limit=0,
                                  tree=False, pick=True, exec_=False, func=cmd_browse)
    if cmd == "leaves":
        return argparse.Namespace(path=path, cmd="leaves", leaves=True, limit=0,
                                  tree=False, pick=True, exec_=False, func=cmd_browse)
    if cmd == "tree":
        return argparse.Namespace(path=path, cmd="tree", match="", func=cmd_tree)
    if cmd == "search":
        return argparse.Namespace(path=path, cmd="search", query=value or "",
                                  limit=0, tree=False, pick=True, exec_=False,
                                  func=cmd_search)
    if cmd == "resume":
        return argparse.Namespace(path=path, cmd="resume", uuid=value or "",
                                  exec_=False, func=cmd_resume)
    if cmd == "info":
        return argparse.Namespace(path=path, cmd="info", uuid=value or "",
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

    # Interactive flow: first pick a project, then pick an action.
    if args.path is not None:
        chosen_path = args.path
    elif sys.stdin.isatty() and sys.stdout.isatty():
        picked = pick_project(os.getcwd())
        if picked is None:
            return  # user quit
        chosen_path = picked
    else:
        chosen_path = os.getcwd()

    choice = interactive_menu(chosen_path) if (sys.stdin.isatty() and sys.stdout.isatty()) else None
    if choice is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            # Non-tty default: dump the list
            args = argparse.Namespace(
                path=chosen_path, cmd="list", leaves=False, limit=0, tree=False,
                pick=False, exec_=False, func=cmd_browse,
            )
            args.func(args)
        return
    cmd, value = choice
    args = _ns_for(cmd, value, chosen_path)
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
