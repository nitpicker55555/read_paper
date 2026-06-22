# treeflow

> **Every branch remembers how it grew.**
> Deep, traceable collaboration with Claude Code and Codex — branch
> endlessly to understand a problem, or grow one project.

Stdlib-only Python CLI to browse and resume any node in your local agent
conversation tree — **including abandoned sibling branches** that stock
`--resume` can't reach natively. Supports two backends:

- **Claude Code** (default): `~/.claude/projects/<slug>/<sid>.jsonl` files.
  Tree across branches in one slug. Resume any historical node.
- **OpenAI Codex CLI** (`--codex`): `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
  rollouts, stitched into a session tree via each session's `forked_from_id`.
  Resume any message-level prompt, even mid-session, even on abandoned branches.

The trick: when you pick a node, `treeflow` writes a fresh rollout file
whose content is just the parent chain from your target up to the root,
with the header rewritten to a brand-new session id. The agent CLI loads
that file via its native `--resume` and lands exactly at your target.

## Install

```bash
# from a clone of the repo, install the package
pip install ./cli

# editable install (changes to cli/treeflow/ take effect immediately)
pip install -e ./cli

# from GitHub without cloning
pip install "git+https://github.com/nitpicker55555/Agent-Treeflow"

# globally, in an isolated environment (recommended for a personal tool)
pipx install ./cli
```

After installation the `treeflow` command is on PATH. `python -m treeflow`
is equivalent.

Pure Python 3.11+ stdlib — no third-party dependencies. (3.11 is the floor
because `tomllib` is used for config parsing.)

### User-level config

The package ships `treeflow.toml` next to the code with sensible defaults
(page size, default tool, default view, tree order, etc.). To override a
setting without editing the installed file, create one of these — keys
missing here fall back to the bundled defaults:

```
~/.config/treeflow/treeflow.toml   # XDG-style, preferred
~/.treeflow.toml                   # also recognized
```

`TREEFLOW_CONFIG=/path/to/treeflow.toml` env var also works.

## Use

Running `treeflow` with no arguments in an interactive terminal opens a
two-step interactive flow: **root picker → unified browser**.

A "project" here means: one root user prompt plus the subtree growing
from it. A single conversation directory typically contains many such
roots — the independent conversations you started in that path. The
picker lists every root in the cwd's project dir, sorted by latest
activity.

After you pick a root, the **browser** opens directly into the tree view
of that root's subtree. It's a single screen with:

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
| `^R` | flip tree direction — newest leaf at top, root at bottom |
| `^A` | toggle search scope: prompts only ↔ prompts + assistant replies |
| `⎋ Esc` | clear search (or back to project picker if empty) |
| `^C` | quit |

Tree view shows the full structural tree with long linear runs collapsed
to `⋮ (N hidden)`. List and leaves views are the same tabular layout as
the older `treeflow list` dump, except interactive. Search highlights
matches inline.

Press `a` inside the **project picker** to widen the scope to every root
across every project dir (handy when cd is in a directory with no project
of its own). Pass `treeflow -a` to start in that mode.

```bash
# interactive: pick root conversation → pick action (Claude by default)
treeflow

# same, but browse your Codex CLI sessions
treeflow --codex
treeflow --tool codex roots --json

# skip both pickers and operate on the whole jsonl dir at cwd
treeflow -p .

# skip both pickers, use a specific path
treeflow -p ~/path/to/your-project

# non-interactive scoping to a specific root by uuid prefix
treeflow -r 79c67796 list
treeflow -r 79c67796 tree

# only branch tails (leaf nodes — usually the most useful targets)
treeflow leaves

# keyword search across every user prompt
treeflow search "browser tools"

# ASCII tree of the whole project (linear runs collapsed to "⋮ N hidden")
treeflow tree
treeflow tree -m "browser"        # highlight matches inside the tree

# any list command can also be rendered as tree (overlay highlight from search)
treeflow list -t
treeflow search "bash" -t

# look up a node by uuid prefix
treeflow resume 79c67796
treeflow info   79c67796

# point at a different project explicitly
treeflow -p ~/some/other/repo

# auto-exec the resume command after picking (instead of just printing it)
treeflow -x
treeflow search "bash" -x
```

Inside any picker: `↑/↓` move, `Enter` select, `1-9` jump to that absolute
position, `⎋` (Esc) / `←` / `Backspace` to go back to the previous layer
(node picker → action menu → project picker), and `q` to quit immediately.

## Agent / scriptable usage

Every subcommand accepts `--json` for machine-readable output. JSON mode
also suppresses the interactive picker, so it's safe to drive treeflow
from a non-interactive agent loop:

```bash
# list all root conversations in a project (one entry per root prompt)
treeflow -p ~/path roots --json

# enumerate every node in a specific root's subtree
treeflow -p ~/path -r 79c67796 list --json

# only the abandoned/active leaves of a subtree
treeflow -p ~/path -r 79c67796 leaves --json

# search across all prompts in the project (or scope with -r)
treeflow -p ~/path search "playwright" --json

# whole-project tree as nested JSON
treeflow -p ~/path tree --json

# look up one node
treeflow -p ~/path info 79c67796 --json

# generate the resume command (synthetic-session if needed) for any node
treeflow -p ~/path resume 79c67796 --json
# {"tool": "...", "target_uuid": "...", "session_id": "<new>", "file": "...", "command": "...", "chain_length": 1386}
```

Errors in `--json` mode are emitted as `{"error": "...", ...}` to stdout
with exit code 2; otherwise they go to stderr with the same exit code.

By default `list / leaves / search` show **every match** (no limit). When
the output exceeds your terminal height and stdout is a tty, `treeflow`
automatically pipes through `less -RFX` (preserves colors, no
alternate-screen on exit, no pager when it fits one screen). Pass `-n N`
to cap the result count.

## Output legend

```
 #  time         uuid      L  ★  prompt
 1  06-10 22:09  b752d314  ·  ★  - (b) 全部 25 个一起跑 3 轮…
```

- `L` (`·`) — leaf node (no children, i.e. a branch tail)
- `★` (purple) — **natively reachable** by stock `--resume <session-id>`:
  - Claude: the latest `last-prompt` event in some session's jsonl
    resolves to this node (and every ancestor on its parentUuid chain)
  - Codex: this prompt is the **last** prompt of some session's rollout
- All other rows are unreachable via stock `--resume`; `treeflow` unlocks
  them by writing a synthetic session file.

In tree mode (`treeflow tree` / `treeflow list -t`) each `●` is a root,
`├─/└─` are branches, and `⋮ (N hidden)` marks a linear chain of `N`
prompts in between that have no forks — collapsed so the topology stays
readable on 1000+ node trees. Press `^R` in the browser to flip the
direction: newest leaf on top, root at the bottom, corner glyphs flipped
to `┌─` so it reads correctly bottom-up.

## How it works (Claude)

Claude Code's `--resume <sid>` walks `parentUuid` backward from the
file's latest `last-prompt` event's `leafUuid` to build the conversation
chain. Abandoned sibling branches stay on disk but never enter that
chain, so the hidden `--resume-session-at <message-uuid>` flag also can't
reach them. `treeflow` sidesteps this by writing a **new** `<sid>.jsonl`
containing only the chain you want (the ancestors of your target, copied
verbatim) plus one synthetic `last-prompt` pointing at your target. To
Claude Code it looks like a normal session whose active leaf happens to
be your target. The file name must match the session id exactly
(`<sid>.jsonl`), or Claude Code reports "No conversation found".

## How it works (Codex)

Codex has no native concept of sibling branches — every session is a
linear rollout, and forks are recorded only via each new session's
`forked_from_id`. `treeflow` stitches them into a tree: each user prompt
is a node, prompts within one session chain linearly, and a session's
first prompt is grafted onto the last prompt of its parent session.

- Node IDs are `<codex-session-id>:<prompt-index>` (0-based). A prefix
  that uniquely identifies a session also uniquely identifies its nodes
  — but if multiple prompts in the same session match a prefix, you'll
  need to add `:N` to disambiguate.
- Native shortcut for Codex: when you pick the **last** prompt of a
  session, `treeflow` emits `codex resume <original-sid>` instead of
  synthesizing a new file — that's where stock `codex resume` already
  lands.
- Each synthesized rollout is placed under
  `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<new-sid>.jsonl`. Codex
  finds it by walking the date dirs for the matching session id.

## Notes

- Each `treeflow resume` (or interactive pick) generates a new file.
  There's no dedup — re-clicking the same node makes another synthetic
  file. Clean up unused ones manually if you care.
- Once you resume into the synthetic session and start chatting, the
  underlying CLI appends to that file like any normal session. It's now
  a real conversation branch.
- For Claude, the slug for a path uses Claude Code's own rule: replace
  `/` and `_` with `-`. e.g. `/Users/puzhen/PycharmProjects/read_paper`
  → `-Users-puzhen-PycharmProjects-read-paper`.
- Override the project root with `CLAUDE_PROJECTS_DIR=/some/other/dir
  treeflow` (or `CODEX_SESSIONS_DIR=/path/to/dir treeflow --codex`).
