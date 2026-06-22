# atr — agent tree resume

Stdlib-only Python CLI to browse and resume any node in your local Claude Code
conversation tree — **including abandoned sibling branches** that
`claude --resume <session-id>` can't reach natively.

The trick: when you pick a node, `atr` writes a fresh
`~/.claude/projects/<slug>/<new-sid>.jsonl` whose content is just the parent
chain from your target back to the root, capped by a synthesized
`last-prompt` event pinning the active leaf at your target. Claude Code
loads that file and lands exactly there.

## Install

```bash
# from the project root
chmod +x cli/atr.py

# put it on PATH (any one of these)
sudo ln -s "$PWD/cli/atr.py" /usr/local/bin/atr
# or
ln -s "$PWD/cli/atr.py" ~/.local/bin/atr
# or just alias in ~/.zshrc
echo "alias atr=\"$PWD/cli/atr.py\"" >> ~/.zshrc
```

No deps — pure Python 3.9+ stdlib.

## Use

Running `atr` with no arguments in an interactive terminal opens a two-step
interactive flow: **root picker → unified browser**.

A "project" here matches the Flask app's meaning: one root user prompt
(parentUuid: null) plus the subtree growing from it. A single jsonl
directory typically contains many such roots — the independent conversations
you started in that path. The picker lists every root in the cwd's project
dir (`~/.claude/projects/<slug>/`), sorted by latest activity.

After you pick a root, the **browser** opens directly into the tree view of
that root's subtree. It's a single screen with:

- a persistent **search box** at the top — just type to filter
- a row of view tabs (`[tree]  list  leaves`) — press `⇥ Tab` to cycle
- a paginated body that updates live as you type or change view

Keys inside the browser:

| key | action |
|---|---|
| any printable char | append to search filter |
| `⌫ Backspace` | erase last search char (or back to project picker if empty) |
| `↑ ↓` | move selection |
| `⏎ Enter` | resume the highlighted node (writes synthetic jsonl, prints command) |
| `⇥ Tab` / `⇧⇥` | cycle view: tree → list → leaves |
| `⎋ Esc` | clear search (or back to project picker if empty) |
| `^C` | quit |

Tree view shows the full structural tree with long linear runs collapsed to
`⋮ (N hidden)`. List and leaves views are the same tabular layout as the
older `atr list` dump, except interactive. Search highlights matches inline.

Press `a` inside the **project picker** to widen the scope to every root
across every project dir (handy when cd is in a directory with no project
of its own). Pass `atr -a` to start in that mode.

```bash
# interactive: pick root conversation → pick action
atr

# skip both pickers and operate on the whole jsonl dir at cwd
atr -p .

# skip both pickers, use a specific path
atr -p ~/path/to/your-project

# non-interactive scoping to a specific root by uuid prefix
atr -r 79c67796 list
atr -r 79c67796 tree

# only branch tails (leaf nodes — usually the most useful targets)
atr leaves

# keyword search across every user prompt
atr search "browser tools"

# ASCII tree of the whole project (linear runs collapsed to "⋮ N hidden")
atr tree
atr tree -m "browser"        # highlight matches inside the tree

# any list command can also be rendered as tree (overlay highlight from search)
atr list -t
atr search "bash" -t

# look up a node by uuid prefix
atr resume 79c67796
atr info   79c67796

# point at a different project explicitly
atr -p ~/some/other/repo

# auto-exec the resume command after picking (instead of just printing it)
atr -x
atr search "bash" -x
```

Inside any picker: `↑/↓` move, `Enter` select, `1-9` jump to that absolute
position, `⎋` (Esc) / `←` / `Backspace` to go back to the previous layer
(node picker → action menu → project picker), and `q` to quit immediately.

## Agent / scriptable usage

Every subcommand accepts `--json` for machine-readable output. JSON mode
also suppresses the interactive picker, so it's safe to drive atr from a
non-interactive agent loop:

```bash
# list all root conversations in a project (one entry per parentUuid:null prompt)
atr -p ~/path roots --json

# enumerate every node in a specific root's subtree
atr -p ~/path -r 79c67796 list --json

# only the abandoned/active leaves of a subtree
atr -p ~/path -r 79c67796 leaves --json

# search across all prompts in the project (or scope with -r)
atr -p ~/path search "playwright" --json

# whole-project tree as nested JSON
atr -p ~/path tree --json

# look up one node
atr -p ~/path info 79c67796 --json

# generate the synthetic-session `claude --resume <new-sid>` command for any node
atr -p ~/path resume 79c67796 --json
# {"target_uuid": "...", "session_id": "<new>", "file": "...", "command": "...", "chain_length": 1386}
```

Errors in `--json` mode are emitted as `{"error": "...", ...}` to stdout
with exit code 2; otherwise they go to stderr with the same exit code.

In the interactive menus, `↑/↓` move, `Enter` selects, `1-9` jumps + confirms,
and `q` / `Esc` quits. Both menus are pure ANSI — no curses, no alt-screen,
no extra deps.

By default `list / leaves / search` show **every match** (no limit). When the
output exceeds your terminal height and stdout is a tty, `atr` automatically
pipes through `less -RFX` (preserves colors, no alternate-screen on exit, no
pager when it fits one screen). Pass `-n N` to cap the result count.

In pick mode the pager is skipped so the prompt lands right after the table —
narrow with `-n` / `search` / `leaves` first if the list is too big to scan.

## Output legend

```
 #  time         uuid      L  ★  prompt
 1  06-10 22:09  b752d314  ·  ★  - (b) 全部 25 个一起跑 3 轮…
```

- `L` (`·`) — leaf node (no children, i.e. a branch tail)
- `★` (purple) — **natively reachable** by `claude --resume <session-id>` —
  the latest `last-prompt` event in some session's jsonl resolves to this node
- All other rows are unreachable via stock `--resume`; `atr` unlocks them

In tree mode (`atr tree` / `atr list -t`) each `●` is a root (a `parentUuid:
null` user prompt — the start of a session), `├─/└─` is a branch, and
`⋮ (N hidden)` marks a linear chain of `N` user prompts in between that have
no forks and no notable nodes — collapsed so the topology stays readable on
1000+ node trees.

A typical project shows the asymmetry: in the magento project this repo has
been working on, the count is **6 native vs 1743 only-via-atr** out of 1749.

## How it works (one paragraph)

Claude Code's `--resume <sid>` walks `parentUuid` backward from the file's
latest `last-prompt` event's `leafUuid` to build the conversation chain.
Abandoned sibling branches stay on disk but never enter that chain, so the
hidden `--resume-session-at <message-uuid>` flag also can't reach them
(`findIndex` against the loaded chain returns -1). `atr` sidesteps this by
writing a **new** `<sid>.jsonl` containing only the chain you want (the
ancestors of your target, copied verbatim) plus one synthetic `last-prompt`
pointing at your target. To Claude Code it looks like a normal session whose
active leaf happens to be your target.

The file name must match the session id exactly (`<sid>.jsonl`), or Claude
Code reports "No conversation found".

## Notes

- Each `atr resume` (or interactive pick) generates a new file. There's no
  dedup — re-clicking the same node makes another synthetic file.
- Clean up later with `rm ~/.claude/projects/<slug>/<sid>.jsonl` for any
  synthetic file you didn't actually use.
- Once you `claude --resume <new-sid>` and start chatting, Claude Code appends
  to that file like any normal session. It's now a real conversation branch.
- The slug for a path uses Claude Code's own rule: replace `/` and `_` with
  `-`. e.g. `/Users/puzhen/PycharmProjects/read_paper`
  → `-Users-puzhen-PycharmProjects-read-paper`.
- Override the project root with `CLAUDE_PROJECTS_DIR=/some/other/dir atr`.
