const NODE_W = 238;
const NODE_H = 112;
const NODE_OVERFLOW = 18;
const X_GAP = 330;
const Y_GAP = 150;
const PALETTE = ["#287c74", "#b6542f", "#3a6ea5", "#6f7d51", "#9b4d68", "#7a6840", "#2f7d51", "#8c5b2e"];
const SIDE_WIDTH_KEY = "sidePanelWidth";
const DEFAULT_SIDE_WIDTH = 420;
const MIN_SIDE_WIDTH = 340;
const MAX_SIDE_WIDTH = 920;

const state = {
  allNodes: [],
  nodes: [],
  projects: [],
  files: [],
  workspaceFiles: [],
  workspaceLoading: false,
  workspaceError: "",
  workspacePreviewFile: null,
  workspaceOpen: localStorage.getItem("workspaceDrawerOpen") === "1",
  workDir: "",
  selectedProjectId: localStorage.getItem("selectedProjectId") || null,
  selectedId: null,
  parentId: null,
  fileIds: new Set(),
  workspaceRefs: [],
  layout: new Map(),
  nodeById: new Map(),
  childrenByParent: new Map(),
  branchColors: new Map(),
  selectedPath: new Set(),
  transform: { x: 90, y: 72, k: 1 },
  search: "",
  treeDirection: localStorage.getItem("treeDirection") === "right" ? "right" : "down",
  dragging: false,
  dragStart: null,
  resizingSidePanel: false,
  sidePanelWidth: DEFAULT_SIDE_WIDTH,
  sending: false,
  activeSideTab: "conversation",
  noteDraftNodeId: null,
  noteDraft: "",
  noteDirty: false,
  noteSaving: false,
  noteEditing: false,
  projectNotes: [],
  projectNotesProjectId: null,
  noteContextText: "",
  scrollConversationToLatest: false,
};

const els = {
  workspaceLabel: document.getElementById("workspaceLabel"),
  projectSelect: document.getElementById("projectSelect"),
  searchInput: document.getElementById("searchInput"),
  directionBtns: document.querySelectorAll("[data-direction]"),
  focusBtn: document.getElementById("focusBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  zoomInBtn: document.getElementById("zoomInBtn"),
  zoomOutBtn: document.getElementById("zoomOutBtn"),
  canvasHost: document.getElementById("canvasHost"),
  treeSvg: document.getElementById("treeSvg"),
  viewport: document.getElementById("viewport"),
  edgeLayer: document.getElementById("edgeLayer"),
  nodeLayer: document.getElementById("nodeLayer"),
  miniMap: document.getElementById("miniMap"),
  emptyState: document.getElementById("emptyState"),
  sideResizeHandle: document.getElementById("sideResizeHandle"),
  sidePanel: document.getElementById("sidePanel"),
  composer: document.getElementById("composer"),
  promptInput: document.getElementById("promptInput"),
  sendBtn: document.getElementById("sendBtn"),
  selectedFiles: document.getElementById("selectedFiles"),
  detailTitle: document.getElementById("detailTitle"),
  detailMeta: document.getElementById("detailMeta"),
  sideTabBtns: document.querySelectorAll("[data-side-tab]"),
  conversationPane: document.getElementById("conversationPane"),
  conversationList: document.getElementById("conversationList"),
  notePane: document.getElementById("notePane"),
  noteStatus: document.getElementById("noteStatus"),
  noteProjectBtn: document.getElementById("noteProjectBtn"),
  noteEditBtn: document.getElementById("noteEditBtn"),
  noteSaveBtn: document.getElementById("noteSaveBtn"),
  noteBox: document.getElementById("noteBox"),
  noteEditor: document.getElementById("noteEditor"),
  noteRendered: document.getElementById("noteRendered"),
  noteContextMenu: document.getElementById("noteContextMenu"),
  noteContextAppendBtn: document.getElementById("noteContextAppendBtn"),
  nodeNotePreview: document.getElementById("nodeNotePreview"),
  nodePromptPreview: document.getElementById("nodePromptPreview"),
  deleteBtn: document.getElementById("deleteBtn"),
  exportBtn: document.getElementById("exportBtn"),
  fileInput: document.getElementById("fileInput"),
  workspaceDrawer: document.getElementById("workspaceDrawer"),
  workspaceToggleBtn: document.getElementById("workspaceToggleBtn"),
  workspaceCollapseBtn: document.getElementById("workspaceCollapseBtn"),
  workspaceRefreshBtn: document.getElementById("workspaceRefreshBtn"),
  workspaceFileList: document.getElementById("workspaceFileList"),
  workspacePreview: document.getElementById("workspacePreview"),
  workspacePreviewBackdrop: document.getElementById("workspacePreviewBackdrop"),
  workspacePreviewCloseBtn: document.getElementById("workspacePreviewCloseBtn"),
  workspacePreviewTitle: document.getElementById("workspacePreviewTitle"),
  workspacePreviewMeta: document.getElementById("workspacePreviewMeta"),
  workspacePreviewBody: document.getElementById("workspacePreviewBody"),
  workspaceOpenBtn: document.getElementById("workspaceOpenBtn"),
  workspaceDownloadBtn: document.getElementById("workspaceDownloadBtn"),
};

function svgEl(name) {
  return document.createElementNS("http://www.w3.org/2000/svg", name);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTokens(value) {
  const n = Number(value || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}m`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

function compact(text, limit = 140) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, limit - 1)}…`;
}

function firstLines(text, limit = 10) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const visible = lines.slice(0, limit);
  if (lines.length > limit) visible.push("...");
  return visible.join("\n").trim();
}

function compactFileName(name, head = 10, tail = 12) {
  const value = String(name || "");
  if (value.length <= head + tail + 3) return value;
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
}

function fileKindForName(name) {
  const lower = String(name || "").toLowerCase();
  if (lower.endsWith(".md") || lower.endsWith(".markdown") || lower.endsWith(".mdown")) return "markdown";
  if (lower.endsWith(".pdf")) return "pdf";
  if (/\.(txt|csv|json|jsonl|log|py|js|ts|css|html|xml|yaml|yml|toml|rst)$/i.test(lower)) return "text";
  return "file";
}

function workspaceFileUrl(fileOrPath, download = false) {
  const relPath = typeof fileOrPath === "string" ? fileOrPath : fileOrPath.path;
  const suffix = download ? "&download=1" : "";
  return `/api/workspace/file?path=${encodeURIComponent(relPath)}${suffix}`;
}

function workspaceContentUrl(fileOrPath) {
  const relPath = typeof fileOrPath === "string" ? fileOrPath : fileOrPath.path;
  return `/api/workspace/content?path=${encodeURIComponent(relPath)}`;
}

function workspaceRelPathFromHref(href) {
  const root = String(state.workDir || "").replace(/\/+$/, "");
  if (!root || !href) return "";
  const candidates = [];
  try {
    candidates.push(decodeURIComponent(String(href)));
  } catch {
    candidates.push(String(href));
  }
  try {
    const url = new URL(href, window.location.origin);
    candidates.push(decodeURIComponent(url.pathname));
  } catch {
    // Non-URL workspace paths are handled by the raw candidate.
  }
  for (const candidate of candidates) {
    const normalized = String(candidate || "").replace(/^file:\/+/, "/");
    if (normalized === root) return "";
    if (normalized.startsWith(`${root}/`)) return normalized.slice(root.length + 1);
  }
  return "";
}

function workspaceFileForPath(relPath) {
  const existing = state.workspaceFiles.find((file) => file.path === relPath);
  if (existing) return existing;
  const name = relPath.split("/").pop() || relPath;
  return {
    path: relPath,
    name,
    dir: relPath.includes("/") ? relPath.slice(0, relPath.lastIndexOf("/")) : "",
    size: 0,
    modified_at: "",
    mime: "",
    kind: fileKindForName(name),
  };
}

function workspaceRefFromFile(file) {
  return {
    path: file.path,
    absolute_path: file.absolute_path || (state.workDir ? `${state.workDir.replace(/\/+$/, "")}/${file.path}` : file.path),
    name: file.name,
    kind: file.kind || fileKindForName(file.name),
    mime: file.mime || "",
    size: Number(file.size || 0),
  };
}

function appendInlineMarkdown(parent, text) {
  const source = String(text || "");
  const pattern = /(\[[^\]]+\]\([^)]+\)|`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let lastIndex = 0;
  let match;

  function appendText(value) {
    if (value) parent.appendChild(document.createTextNode(value));
  }

  while ((match = pattern.exec(source))) {
    appendText(source.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.appendChild(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.appendChild(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      parent.appendChild(emphasis);
    } else {
      const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (!linkMatch) {
        appendText(token);
      } else {
        const [, label, href] = linkMatch;
        let safeUrl = null;
        const workspaceRelPath = workspaceRelPathFromHref(href);
        try {
          const url = new URL(href, window.location.origin);
          if (["http:", "https:", "mailto:"].includes(url.protocol)) safeUrl = url.href;
        } catch {
          safeUrl = null;
        }
        if (workspaceRelPath) {
          const link = document.createElement("a");
          const file = workspaceFileForPath(workspaceRelPath);
          link.href = workspaceFileUrl(file);
          link.textContent = label;
          link.addEventListener("click", (event) => {
            event.preventDefault();
            openWorkspaceFile(file).catch((err) => window.alert(err.message));
          });
          parent.appendChild(link);
        } else if (!safeUrl) {
          appendText(label);
        } else {
          const link = document.createElement("a");
          link.href = safeUrl;
          link.textContent = label;
          if (!safeUrl.startsWith(window.location.origin)) {
            link.target = "_blank";
            link.rel = "noopener noreferrer";
          }
          parent.appendChild(link);
        }
      }
    }
    lastIndex = pattern.lastIndex;
  }
  appendText(source.slice(lastIndex));
}

function renderMarkdown(text) {
  const root = document.createElement("div");
  root.className = "markdown-body";
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let list = null;
  let listType = "";

  function flushParagraph() {
    const content = paragraph.join(" ").trim();
    paragraph = [];
    if (!content) return;
    const p = document.createElement("p");
    appendInlineMarkdown(p, content);
    root.appendChild(p);
  }

  function resetList() {
    list = null;
    listType = "";
  }

  function ensureList(type) {
    if (list && listType === type) return list;
    flushParagraph();
    list = document.createElement(type);
    listType = type;
    root.appendChild(list);
    return list;
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    const fence = trimmed.match(/^```(.*)$/);
    if (fence) {
      flushParagraph();
      resetList();
      const language = fence[1].trim();
      const codeLines = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i += 1;
      }
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (language) code.dataset.language = language;
      code.textContent = codeLines.join("\n");
      pre.appendChild(code);
      root.appendChild(pre);
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      resetList();
      continue;
    }

    if (/^---+$/.test(trimmed)) {
      flushParagraph();
      resetList();
      root.appendChild(document.createElement("hr"));
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      resetList();
      const level = Math.min(heading[1].length, 6);
      const h = document.createElement(`h${level}`);
      appendInlineMarkdown(h, heading[2]);
      root.appendChild(h);
      continue;
    }

    const quote = trimmed.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      resetList();
      const blockquote = document.createElement("blockquote");
      const quoted = [];
      while (i < lines.length) {
        const quoteLine = lines[i].trim().match(/^>\s?(.*)$/);
        if (!quoteLine) break;
        quoted.push(quoteLine[1]);
        i += 1;
      }
      i -= 1;
      appendInlineMarkdown(blockquote, quoted.join(" "));
      root.appendChild(blockquote);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      const ul = ensureList("ul");
      const li = document.createElement("li");
      appendInlineMarkdown(li, unordered[1]);
      ul.appendChild(li);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const ol = ensureList("ol");
      const li = document.createElement("li");
      appendInlineMarkdown(li, ordered[1]);
      ol.appendChild(li);
      continue;
    }

    resetList();
    paragraph.push(line);
  }

  flushParagraph();
  if (!root.childElementCount && text) {
    const p = document.createElement("p");
    p.textContent = String(text);
    root.appendChild(p);
  }
  return root;
}

function statusText(status) {
  return {
    queued: "排队",
    running: "运行",
    done: "完成",
    failed: "失败",
  }[status] || status || "-";
}

function isNodeActive(node) {
  return node && ["queued", "running"].includes(node.status);
}

function selectedNode() {
  return state.nodeById.get(state.selectedId) || null;
}

function parentNode() {
  return state.nodeById.get(state.parentId) || null;
}

async function fetchTree({ keepSelection = true } = {}) {
  const res = await fetch("/api/tree");
  const data = await res.json();
  state.allNodes = data.nodes || [];
  state.files = data.files || [];
  state.workDir = data.work_dir || "";
  rebuildProjects();
  ensureSelectedProject(keepSelection);
  state.nodes = nodesForProject(state.selectedProjectId);
  rebuildIndexes();

  if (!keepSelection || (state.selectedId && !state.nodeById.has(state.selectedId))) {
    state.selectedId = state.selectedProjectId === "__new__" ? null : state.selectedProjectId;
  }
  if (state.parentId && !state.nodeById.has(state.parentId)) {
    state.parentId = state.selectedId;
  }
  if (!state.selectedId && state.nodes.length) {
    state.selectedId = state.nodes[0].id;
    state.parentId = state.selectedId;
  }
  render();
}

function rebuildProjects() {
  const allById = new Map(state.allNodes.map((node) => [node.id, node]));
  state.projects = state.allNodes
    .filter((node) => !node.parent_id || !allById.has(node.parent_id))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
}

function ensureSelectedProject(keepSelection) {
  const valid = state.projects.some((project) => project.id === state.selectedProjectId);
  if (state.selectedProjectId === "__new__") return;
  if (!keepSelection || !valid) {
    state.selectedProjectId = state.projects.length ? state.projects[0].id : "__new__";
    localStorage.setItem("selectedProjectId", state.selectedProjectId);
  }
}

function nodesForProject(projectId) {
  if (!projectId || projectId === "__new__") return [];
  const allByParent = new Map();
  for (const node of state.allNodes) {
    const key = node.parent_id || "__root__";
    if (!allByParent.has(key)) allByParent.set(key, []);
    allByParent.get(key).push(node);
  }
  const result = [];
  const queue = [projectId];
  const seen = new Set();
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const node = state.allNodes.find((item) => item.id === id);
    if (!node) continue;
    result.push(node);
    for (const child of allByParent.get(id) || []) {
      queue.push(child.id);
    }
  }
  return result.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
}

function rebuildIndexes() {
  state.nodeById = new Map(state.nodes.map((node) => [node.id, node]));
  state.childrenByParent = new Map();
  for (const node of state.nodes) {
    const key = node.parent_id || "__root__";
    if (!state.childrenByParent.has(key)) state.childrenByParent.set(key, []);
    state.childrenByParent.get(key).push(node);
  }
  for (const children of state.childrenByParent.values()) {
    children.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  }
}

function render() {
  layoutTree();
  computeSelectedPath();
  assignBranchColors();
  applyTransform();
  renderTree();
  renderMiniMap();
  renderConversation();
  renderSideTabs();
  renderNotePanel();
  renderComposer();
  renderWorkspaceFiles();
  renderWorkspaceDrawer();
  renderProjectSelect();
  renderDirectionControls();
  els.workspaceLabel.textContent = state.workDir ? state.workDir : "";
  els.emptyState.classList.toggle("visible", state.nodes.length === 0);
}

function renderWorkspaceDrawer() {
  if (!els.workspaceDrawer) return;
  els.workspaceDrawer.classList.toggle("collapsed", !state.workspaceOpen);
  els.workspaceToggleBtn.setAttribute("aria-label", state.workspaceOpen ? "折叠 Workspace" : "展开 Workspace");
  els.workspaceToggleBtn.title = state.workspaceOpen ? "折叠 Workspace" : "展开 Workspace";
}

function setWorkspaceDrawerOpen(open) {
  state.workspaceOpen = open;
  localStorage.setItem("workspaceDrawerOpen", open ? "1" : "0");
  renderWorkspaceDrawer();
  setTimeout(() => {
    renderMiniMap();
  }, 180);
}

function renderProjectSelect() {
  const currentValue = state.selectedProjectId || "__new__";
  const options = [];
  const newOption = document.createElement("option");
  newOption.value = "__new__";
  newOption.textContent = "say something";
  options.push(newOption);
  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = compact(project.title || project.prompt || "未命名项目", 38);
    options.push(option);
  }
  els.projectSelect.replaceChildren(...options);
  els.projectSelect.value = currentValue;
  els.projectSelect.classList.toggle("placeholder", currentValue === "__new__");
}

function layoutTree() {
  state.layout = new Map();
  const roots = state.nodes.filter((node) => !node.parent_id || !state.nodeById.has(node.parent_id));
  let leaf = 0;
  const visited = new Set();

  function walk(node, depth) {
    const breadthGap = state.treeDirection === "right" ? Y_GAP : X_GAP;
    if (!node || visited.has(node.id)) return leaf * breadthGap;
    visited.add(node.id);
    const children = state.childrenByParent.get(node.id) || [];
    let breadth;
    if (!children.length) {
      breadth = leaf * breadthGap;
      leaf += 1;
    } else {
      const positions = children.map((child) => walk(child, depth + 1));
      breadth = positions.reduce((sum, item) => sum + item, 0) / positions.length;
    }
    const pos = state.treeDirection === "right"
      ? { x: depth * X_GAP, y: breadth, depth }
      : { x: breadth, y: depth * Y_GAP, depth };
    state.layout.set(node.id, pos);
    return breadth;
  }

  for (const root of roots) {
    walk(root, 0);
    leaf += 0.6;
  }
}

function computeSelectedPath() {
  state.selectedPath = new Set();
  let current = selectedNode();
  while (current) {
    state.selectedPath.add(current.id);
    current = state.nodeById.get(current.parent_id);
  }
}

function branchKey(node) {
  let current = node;
  let parent = state.nodeById.get(current.parent_id);
  while (parent && parent.parent_id) {
    current = parent;
    parent = state.nodeById.get(parent.parent_id);
  }
  return current.id;
}

function assignBranchColors() {
  state.branchColors = new Map();
  let index = 0;
  const ordered = [...state.nodes].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  for (const node of ordered) {
    const key = branchKey(node);
    if (!state.branchColors.has(key)) {
      state.branchColors.set(key, PALETTE[index % PALETTE.length]);
      index += 1;
    }
  }
}

function colorFor(node) {
  return state.branchColors.get(branchKey(node)) || PALETTE[0];
}

function matchesSearch(node) {
  const query = state.search.trim().toLowerCase();
  if (!query) return true;
  const text = `${node.title || ""} ${node.prompt || ""} ${node.answer || ""}`.toLowerCase();
  return text.includes(query);
}

function shouldDimNode(node) {
  if (state.search && !matchesSearch(node)) return true;
  if (isNodeActive(node)) return false;
  if (state.selectedId && state.selectedPath.size && !state.selectedPath.has(node.id)) {
    const selected = selectedNode();
    return selected && node.parent_id !== selected.id && selected.parent_id !== node.id;
  }
  return false;
}

function renderTree() {
  els.edgeLayer.replaceChildren();
  els.nodeLayer.replaceChildren();

  for (const node of state.nodes) {
    if (!node.parent_id) continue;
    const parent = state.nodeById.get(node.parent_id);
    if (!parent) continue;
    const from = state.layout.get(parent.id);
    const to = state.layout.get(node.id);
    if (!from || !to) continue;
    const path = svgEl("path");
    if (state.treeDirection === "right") {
      const sx = from.x + NODE_W;
      const sy = from.y + NODE_H / 2;
      const tx = to.x;
      const ty = to.y + NODE_H / 2;
      const midX = sx + (tx - sx) / 2;
      path.setAttribute("d", `M${sx},${sy} C${midX},${sy} ${midX},${ty} ${tx},${ty}`);
    } else {
      const sx = from.x + NODE_W / 2;
      const sy = from.y + NODE_H;
      const tx = to.x + NODE_W / 2;
      const ty = to.y;
      const midY = sy + (ty - sy) / 2;
      path.setAttribute("d", `M${sx},${sy} C${sx},${midY} ${tx},${midY} ${tx},${ty}`);
    }
    path.setAttribute("class", edgeClass(parent, node));
    path.style.setProperty("--edge-color", colorFor(node));
    els.edgeLayer.appendChild(path);
  }

  for (const node of state.nodes) {
    const pos = state.layout.get(node.id);
    if (!pos) continue;
    const foreign = svgEl("foreignObject");
    foreign.setAttribute("x", pos.x - NODE_OVERFLOW);
    foreign.setAttribute("y", pos.y - NODE_OVERFLOW);
    foreign.setAttribute("width", NODE_W + NODE_OVERFLOW * 2);
    foreign.setAttribute("height", NODE_H + NODE_OVERFLOW * 2);
    const card = makeNodeCard(node);
    card.style.margin = `${NODE_OVERFLOW}px`;
    foreign.appendChild(card);
    els.nodeLayer.appendChild(foreign);
  }
}

function edgeClass(parent, node) {
  const active = state.selectedPath.has(parent.id) && state.selectedPath.has(node.id);
  const dimmed = state.selectedId && !active;
  return ["edge", active ? "active" : "", dimmed ? "dimmed" : ""].filter(Boolean).join(" ");
}

function positionNodeNotePreview(target) {
  const rect = target.getBoundingClientRect();
  const preview = els.nodeNotePreview;
  const margin = 10;
  const width = preview.offsetWidth || 260;
  const height = preview.offsetHeight || 160;
  const left = clamp(rect.left, margin, window.innerWidth - width - margin);
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - margin) {
    top = rect.top - height - 8;
  }
  preview.style.left = `${left}px`;
  preview.style.top = `${clamp(top, margin, window.innerHeight - height - margin)}px`;
}

function showNodeNotePreview(node, target) {
  const note = firstLines(noteTextForNode(node), 10);
  if (!note) return;
  els.nodeNotePreview.replaceChildren(renderMarkdown(note));
  els.nodeNotePreview.hidden = false;
  positionNodeNotePreview(target);
}

function hideNodeNotePreview() {
  els.nodeNotePreview.hidden = true;
}

function noteTextForNode(node) {
  if (!node) return "";
  if (state.noteDraftNodeId === node.id) return state.noteDraft || "";
  return node.note_md || "";
}

function positionNodePromptPreview(target) {
  const rect = target.getBoundingClientRect();
  const preview = els.nodePromptPreview;
  const margin = 10;
  const width = preview.offsetWidth || 360;
  const height = preview.offsetHeight || 220;
  const left = clamp(rect.left, margin, window.innerWidth - width - margin);
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - margin) {
    top = rect.top - height - 8;
  }
  preview.style.left = `${left}px`;
  preview.style.top = `${clamp(top, margin, window.innerHeight - height - margin)}px`;
}

function showNodePromptPreview(node, target) {
  const prompt = String(node.prompt || "").trim();
  if (!prompt) return;
  const pre = document.createElement("pre");
  pre.textContent = prompt;
  els.nodePromptPreview.replaceChildren(pre);
  els.nodePromptPreview.hidden = false;
  positionNodePromptPreview(target);
}

function hideNodePromptPreview() {
  els.nodePromptPreview.hidden = true;
}

function openNodeNote(nodeId) {
  state.activeSideTab = "notes";
  hideNodePromptPreview();
  selectNode(nodeId);
}

function makeNodeCard(node) {
  const card = document.createElement("div");
  card.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
  card.className = [
    "node-card",
    node.id === state.selectedId ? "selected" : "",
    shouldDimNode(node) ? "dimmed" : "",
    isNodeActive(node) ? "running" : "",
  ]
    .filter(Boolean)
    .join(" ");
  card.style.setProperty("--node-color", colorFor(node));
  card.dataset.nodeId = node.id;
  card.addEventListener("mouseenter", (event) => {
    if (event.target.closest && event.target.closest(".node-note-button")) return;
    showNodePromptPreview(node, card);
  });
  card.addEventListener("mousemove", (event) => {
    if (event.target.closest && event.target.closest(".node-note-button")) {
      hideNodePromptPreview();
      return;
    }
    positionNodePromptPreview(card);
  });
  card.addEventListener("mouseleave", hideNodePromptPreview);

  const noteText = String(noteTextForNode(node)).trim();
  if (noteText) {
    const noteButton = document.createElement("button");
    noteButton.type = "button";
    noteButton.className = "node-note-button";
    noteButton.setAttribute("aria-label", "笔记");
    noteButton.setAttribute("title", "");
    noteButton.textContent = "笔";
    noteButton.addEventListener("mouseenter", (event) => {
      hideNodePromptPreview();
      showNodeNotePreview(node, event.currentTarget);
    });
    noteButton.addEventListener("mousemove", (event) => positionNodeNotePreview(event.currentTarget));
    noteButton.addEventListener("mouseleave", () => {
      hideNodeNotePreview();
    });
    noteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      hideNodeNotePreview();
      openNodeNote(node.id);
    });
    card.appendChild(noteButton);
  }

  const top = document.createElement("div");
  top.className = "node-top";
  const title = document.createElement("div");
  title.className = "node-title";
  title.textContent = node.title || "节点";
  const markers = document.createElement("div");
  markers.className = "node-markers";
  const dot = document.createElement("span");
  dot.className = `status-dot status-${node.status || "queued"}`;
  markers.appendChild(dot);
  top.append(title, markers);

  const snippet = document.createElement("div");
  snippet.className = "node-snippet";
  snippet.textContent = compact(node.answer || node.error || node.prompt, 120);

  const bottom = document.createElement("div");
  bottom.className = "node-bottom";
  const badges = document.createElement("div");
  badges.className = "node-badges";
  const children = state.childrenByParent.get(node.id) || [];
  for (const value of [`${formatTokens(node.total_tokens)} tok`, `${node.tools_count || 0} tool`, `${children.length} fork`]) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = value;
    badges.appendChild(badge);
  }
  const status = document.createElement("span");
  status.textContent = statusText(node.status);
  bottom.append(badges, status);
  const content = document.createElement("div");
  content.className = "node-content";
  content.append(top, snippet, bottom);
  card.appendChild(content);

  card.addEventListener("click", (event) => {
    event.stopPropagation();
    selectNode(node.id);
  });
  card.addEventListener("dblclick", (event) => {
    event.stopPropagation();
    setParent(node.id);
  });
  return card;
}

function selectNode(id) {
  hideNodePromptPreview();
  hideNodeNotePreview();
  state.selectedId = id;
  state.parentId = id;
  render();
  fetchWorkspaceFiles().catch(console.error);
}

function setParent(id) {
  state.parentId = id || null;
  state.selectedId = id || state.selectedId;
  renderComposer();
  els.promptInput.focus();
}

function applyTransform() {
  els.viewport.setAttribute("transform", `translate(${state.transform.x} ${state.transform.y}) scale(${state.transform.k})`);
}

function zoomAt(factor, clientX, clientY) {
  const rect = els.treeSvg.getBoundingClientRect();
  const px = clientX - rect.left;
  const py = clientY - rect.top;
  const beforeX = (px - state.transform.x) / state.transform.k;
  const beforeY = (py - state.transform.y) / state.transform.k;
  const nextK = clamp(state.transform.k * factor, 0.28, 2.4);
  state.transform.x = px - beforeX * nextK;
  state.transform.y = py - beforeY * nextK;
  state.transform.k = nextK;
  applyTransform();
  renderMiniMap();
}

function focusSelected() {
  if (!state.layout.size) return;
  const target = state.selectedId && state.layout.get(state.selectedId) ? state.layout.get(state.selectedId) : boundsCenter();
  const rect = els.canvasHost.getBoundingClientRect();
  state.transform.x = rect.width / 2 - (target.x + NODE_W / 2) * state.transform.k;
  state.transform.y = rect.height / 2 - (target.y + NODE_H / 2) * state.transform.k;
  applyTransform();
  renderMiniMap();
}

function boundsCenter() {
  const bounds = treeBounds();
  return {
    x: bounds.x + bounds.w / 2 - NODE_W / 2,
    y: bounds.y + bounds.h / 2 - NODE_H / 2,
  };
}

function treeBounds() {
  if (!state.layout.size) return { x: 0, y: 0, w: NODE_W, h: NODE_H };
  const xs = [];
  const ys = [];
  for (const pos of state.layout.values()) {
    xs.push(pos.x, pos.x + NODE_W);
    ys.push(pos.y, pos.y + NODE_H);
  }
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function renderMiniMap() {
  const canvas = els.miniMap;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 180;
  const cssH = canvas.clientHeight || 116;
  if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "rgba(255, 253, 250, 0.92)";
  ctx.fillRect(0, 0, cssW, cssH);
  if (!state.layout.size) return;
  const bounds = treeBounds();
  const pad = 12;
  const scale = Math.min((cssW - pad * 2) / Math.max(bounds.w, 1), (cssH - pad * 2) / Math.max(bounds.h, 1));
  const tx = pad - bounds.x * scale;
  const ty = pad - bounds.y * scale;

  ctx.lineWidth = 1;
  for (const node of state.nodes) {
    const pos = state.layout.get(node.id);
    if (!pos) continue;
    ctx.fillStyle = node.id === state.selectedId ? colorFor(node) : "rgba(32, 33, 36, 0.34)";
    ctx.fillRect(tx + pos.x * scale, ty + pos.y * scale, Math.max(3, NODE_W * scale), Math.max(3, NODE_H * scale));
  }

  const host = els.canvasHost.getBoundingClientRect();
  const vx = (-state.transform.x / state.transform.k) * scale + tx;
  const vy = (-state.transform.y / state.transform.k) * scale + ty;
  const vw = (host.width / state.transform.k) * scale;
  const vh = (host.height / state.transform.k) * scale;
  ctx.strokeStyle = "#202124";
  ctx.strokeRect(vx, vy, vw, vh);
}

function selectedConversationPath() {
  const path = [];
  let current = selectedNode();
  const seen = new Set();
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    path.push(current);
    current = state.nodeById.get(current.parent_id);
  }
  return path.reverse();
}

function describeTool(item) {
  const type = item.type || "tool";
  if (type === "command_execution") {
    return {
      name: "Bash",
      detail: item.command || "准备执行命令",
      output: item.aggregated_output || "",
      status: item.status || "in_progress",
    };
  }
  if (type === "web_search") {
    const action = item.action || {};
    const query = item.query || action.query || (Array.isArray(action.queries) ? action.queries.join(", ") : "");
    return {
      name: "Search",
      detail: query || "准备搜索",
      output: "",
      status: item.status || "in_progress",
    };
  }
  return {
    name: type.replaceAll("_", " "),
    detail: item.command || item.name || item.query || "",
    output: item.aggregated_output || "",
    status: item.status || "in_progress",
  };
}

function toolCallsForNode(node) {
  const calls = new Map();
  for (const event of node.raw_events || []) {
    const item = event.item;
    if (!item || typeof item !== "object") continue;
    const type = item.type || "";
    if (!["command_execution", "web_search"].includes(type) && !type.includes("tool")) continue;
    const key = item.id || `${type}-${calls.size}`;
    const described = describeTool(item);
    const eventType = event.type || "";
    calls.set(key, {
      ...described,
      status: eventType === "item.completed" ? "completed" : described.status,
    });
  }
  return [...calls.values()];
}

function renderToolCalls(tools) {
  if (!tools.length) return null;
  const list = document.createElement("div");
  list.className = "tool-list";
  for (const tool of tools) {
    const row = document.createElement("div");
    row.className = `tool-call ${tool.status === "completed" ? "completed" : "running"}`;
    const spinner = document.createElement("span");
    spinner.className = "tool-spinner";
    const text = document.createElement("div");
    text.className = "tool-text";
    const title = document.createElement("div");
    title.className = "tool-title";
    title.textContent = tool.status === "completed" ? `${tool.name} 完成` : `正在调用 ${tool.name}`;
    const detail = document.createElement("div");
    detail.className = "tool-detail";
    detail.textContent = tool.detail || "-";
    text.append(title, detail);
    if (tool.output) {
      const output = document.createElement("pre");
      output.className = "tool-output";
      output.textContent = compact(tool.output, 260);
      text.appendChild(output);
    }
    row.append(spinner, text);
    list.appendChild(row);
  }
  return list;
}

function makeTypingIndicator() {
  const dots = document.createElement("div");
  dots.className = "typing-dots";
  dots.setAttribute("aria-label", "Agent 正在运行");
  for (let i = 0; i < 3; i += 1) {
    const dot = document.createElement("span");
    dots.appendChild(dot);
  }
  return dots;
}

function renderReferenceChips(attachments = [], workspaceRefs = []) {
  const refs = [
    ...attachments.map((item) => ({
      name: item.name || item.original_name || "",
      kind: fileKindForName(item.name || item.original_name || ""),
    })),
    ...workspaceRefs,
  ];
  if (!refs.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "bubble-ref-list";
  for (const ref of refs) {
    wrap.appendChild(makeCompactFileChip(ref, false));
  }
  return wrap;
}

function makeBubble(role, text, meta = "", tools = [], active = false, attachments = [], workspaceRefs = []) {
  const row = document.createElement("div");
  row.className = ["bubble-row", role, active && role === "agent" ? "running" : ""].filter(Boolean).join(" ");
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent = role === "user" ? "User" : "Agent";
  const toolList = role === "agent" ? renderToolCalls(tools) : null;
  bubble.appendChild(label);
  if (toolList) bubble.appendChild(toolList);
  if (role === "agent") {
    bubble.appendChild(renderMarkdown(text || ""));
  } else {
    const body = document.createElement("pre");
    body.textContent = text || "";
    bubble.appendChild(body);
    const refs = renderReferenceChips(attachments, workspaceRefs);
    if (refs) bubble.appendChild(refs);
  }
  if (active && role === "agent") bubble.appendChild(makeTypingIndicator());
  if (meta) {
    const foot = document.createElement("div");
    foot.className = "bubble-meta";
    foot.textContent = meta;
    bubble.appendChild(foot);
  }
  row.appendChild(bubble);
  return row;
}

function setSideTab(tab) {
  state.activeSideTab = tab === "notes" ? "notes" : "conversation";
  hideNoteContextMenu();
  renderSideTabs();
  renderNotePanel();
}

function renderSideTabs() {
  for (const button of els.sideTabBtns) {
    const active = button.dataset.sideTab === state.activeSideTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  els.conversationPane.hidden = state.activeSideTab !== "conversation";
  els.notePane.hidden = state.activeSideTab !== "notes";
}

function ensureNoteDraft(node) {
  if (!node) {
    state.noteDraftNodeId = null;
    state.noteDraft = "";
    state.noteDirty = false;
    state.noteEditing = false;
    return;
  }
  if (state.noteDraftNodeId !== node.id) {
    state.noteDraftNodeId = node.id;
    state.noteDraft = node.note_md || "";
    state.noteDirty = false;
    state.noteEditing = false;
    state.projectNotes = [];
    state.projectNotesProjectId = null;
  }
}

function renderProjectNotesSummary() {
  if (!state.projectNotesProjectId) return null;
  const wrap = document.createElement("section");
  wrap.className = "project-notes-summary";
  const title = document.createElement("h3");
  title.textContent = "项目笔记";
  wrap.appendChild(title);
  if (!state.projectNotes.length) {
    const empty = document.createElement("div");
    empty.className = "soft-label";
    empty.textContent = "暂无笔记";
    wrap.appendChild(empty);
    return wrap;
  }
  for (const item of state.projectNotes) {
    const article = document.createElement("article");
    article.className = "project-note-item";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "project-note-title";
    button.textContent = item.title || "节点";
    button.addEventListener("click", () => {
      selectNode(item.id);
      setSideTab("notes");
    });
    article.appendChild(button);
    article.appendChild(renderMarkdown(item.note_md || ""));
    wrap.appendChild(article);
  }
  return wrap;
}

function renderNotePanel() {
  const node = selectedNode();
  ensureNoteDraft(node);
  const disabled = !node;
  els.noteEditor.disabled = disabled || state.noteSaving;
  els.noteEditBtn.disabled = disabled || state.noteSaving;
  els.noteSaveBtn.disabled = disabled || state.noteSaving || !state.noteEditing;
  els.noteEditBtn.hidden = disabled || state.noteEditing;
  els.noteSaveBtn.hidden = disabled || !state.noteEditing;
  els.noteProjectBtn.disabled = !state.selectedProjectId || state.selectedProjectId === "__new__";
  els.noteStatus.textContent = disabled
    ? "say something"
    : state.noteSaving
      ? "保存中"
      : state.noteDirty
        ? "节点笔记未保存"
        : state.noteEditing
          ? "编辑笔记"
          : "节点笔记";
  if (els.noteEditor.value !== state.noteDraft) {
    els.noteEditor.value = state.noteDraft;
  }
  els.noteEditor.hidden = !state.noteEditing;
  els.noteRendered.hidden = state.noteEditing;

  const rendered = document.createElement("div");
  rendered.className = "note-preview-section";
  const noteText = state.noteDraft.trim();
  if (noteText) {
    rendered.appendChild(renderMarkdown(noteText));
  } else {
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "say something";
    rendered.appendChild(empty);
  }
  const projectNotes = renderProjectNotesSummary();
  if (projectNotes) rendered.appendChild(projectNotes);
  els.noteRendered.replaceChildren(rendered);
}

function renderConversation() {
  const node = selectedNode();
  const disabled = !node;
  els.deleteBtn.disabled = disabled;
  els.exportBtn.disabled = disabled;

  if (!node) {
    els.detailTitle.textContent = "say something";
    els.detailMeta.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "say something";
    els.conversationList.replaceChildren(empty);
    return;
  }

  els.detailTitle.textContent = node.title || "节点";

  const meta = [
    statusText(node.status),
    `${formatTokens(node.total_tokens)} tokens`,
    `${node.tools_count || 0} tools`,
    node.completed_at || node.created_at || "",
  ].filter(Boolean);
  els.detailMeta.replaceChildren(...meta.map((item) => {
    const span = document.createElement("span");
    span.className = "meta-pill";
    span.textContent = item;
    return span;
  }));

  const bubbles = [];
  for (const item of selectedConversationPath()) {
    bubbles.push(makeBubble("user", item.prompt, item.created_at || "", [], false, item.attachments || [], item.workspace_refs || []));
    const active = isNodeActive(item);
    const agentText = item.answer || item.error || (active ? "正在处理" : item.status === "done" ? "" : statusText(item.status));
    bubbles.push(makeBubble("agent", agentText, item.completed_at || item.updated_at || "", toolCallsForNode(item), active));
  }
  els.conversationList.replaceChildren(...bubbles);
  if (state.scrollConversationToLatest) {
    els.conversationList.scrollTop = els.conversationList.scrollHeight;
    state.scrollConversationToLatest = false;
  }
}

function renderComposer() {
  els.selectedFiles.replaceChildren();
  for (const fileId of state.fileIds) {
    const file = state.files.find((item) => item.id === fileId);
    if (!file) continue;
    const chip = makeCompactFileChip(
      {
        name: file.original_name,
        kind: fileKindForName(file.original_name),
      },
      true,
      () => {
        state.fileIds.delete(fileId);
        renderComposer();
      }
    );
    els.selectedFiles.appendChild(chip);
  }
  for (const ref of state.workspaceRefs) {
    const chip = makeCompactFileChip(ref, true, () => {
      state.workspaceRefs = state.workspaceRefs.filter((item) => item.path !== ref.path);
      renderComposer();
    });
    els.selectedFiles.appendChild(chip);
  }
  els.sendBtn.disabled = state.sending;
}

function workspaceIcon(file) {
  if (file.kind === "markdown") return "MD";
  if (file.kind === "pdf") return "PDF";
  if (file.kind === "text") return "TXT";
  return "FILE";
}

function compactFileIcon(file) {
  const kind = file.kind || fileKindForName(file.original_name || file.name);
  if (kind === "markdown") return "M";
  if (kind === "pdf") return "P";
  if (kind === "text") return "T";
  return "F";
}

function makeCompactFileChip(file, removable = false, onRemove = null) {
  const chip = document.createElement("span");
  chip.className = "file-chip compact-file-chip";
  chip.title = file.name || file.original_name || "";
  const icon = document.createElement("span");
  icon.className = "file-icon";
  icon.textContent = compactFileIcon(file);
  const name = document.createElement("span");
  name.textContent = compactFileName(file.name || file.original_name || "");
  chip.append(icon, name);
  if (removable) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.addEventListener("click", onRemove);
    chip.appendChild(remove);
  }
  return chip;
}

function renderWorkspaceFiles() {
  if (!els.workspaceFileList) return;
  if (state.workspaceLoading) {
    const loading = document.createElement("div");
    loading.className = "soft-label";
    loading.textContent = "正在读取 Workspace";
    els.workspaceFileList.replaceChildren(loading);
    return;
  }
  if (state.workspaceError) {
    const error = document.createElement("div");
    error.className = "soft-label error-label";
    error.textContent = state.workspaceError;
    els.workspaceFileList.replaceChildren(error);
    return;
  }
  if (!state.workspaceFiles.length) {
    const empty = document.createElement("div");
    empty.className = "soft-label";
    empty.textContent = "暂无 Workspace 文件";
    els.workspaceFileList.replaceChildren(empty);
    return;
  }

  const rows = state.workspaceFiles.map((file) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "workspace-file-row";
    button.draggable = true;
    const icon = document.createElement("span");
    icon.className = `workspace-file-icon kind-${file.kind || "file"}`;
    icon.textContent = workspaceIcon(file);
    const main = document.createElement("span");
    main.className = "workspace-file-main";
    const name = document.createElement("span");
    name.className = "workspace-file-name";
    name.textContent = file.name;
    const sub = document.createElement("span");
    sub.className = "workspace-file-sub";
    sub.textContent = [
      file.source_title ? `来自 ${compact(file.source_title, 22)}` : "",
      file.dir || "workspace",
      formatBytes(file.size),
      file.modified_at || "",
    ].filter(Boolean).join(" · ");
    main.append(name, sub);
    button.append(icon, main);
    button.addEventListener("dragstart", (event) => {
      const reference = workspaceRefFromFile(file);
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-workspace-ref", JSON.stringify(reference));
      event.dataTransfer.setData("text/plain", file.name);
      event.dataTransfer.setData("text/uri-list", file.absolute_path || file.path);
    });
    button.addEventListener("click", () => {
      openWorkspaceFile(file).catch((err) => window.alert(err.message));
    });
    return button;
  });
  els.workspaceFileList.replaceChildren(...rows);
}

function closeWorkspacePreview() {
  state.workspacePreviewFile = null;
  els.workspacePreview.hidden = true;
  els.workspacePreviewBody.replaceChildren();
}

function renderWorkspacePreviewShell(file) {
  state.workspacePreviewFile = file;
  els.workspacePreview.hidden = false;
  els.workspacePreviewTitle.textContent = file.name;
  els.workspacePreviewMeta.textContent = [file.path, formatBytes(file.size), file.mime || ""].filter(Boolean).join(" · ");
  els.workspaceOpenBtn.href = workspaceFileUrl(file);
  els.workspaceDownloadBtn.href = workspaceFileUrl(file, true);
  const loading = document.createElement("div");
  loading.className = "workspace-preview-loading";
  loading.textContent = "正在加载";
  els.workspacePreviewBody.replaceChildren(loading);
}

async function openWorkspaceFile(file) {
  renderWorkspacePreviewShell(file);

  if (file.kind === "markdown" || file.kind === "text") {
    const res = await fetch(workspaceContentUrl(file));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "读取文件失败");
    const body = document.createElement("div");
    if (file.kind === "markdown") {
      body.className = "workspace-markdown-preview";
      body.appendChild(renderMarkdown(data.content || ""));
    } else {
      const pre = document.createElement("pre");
      pre.className = "workspace-text-preview";
      pre.textContent = data.content || "";
      body.appendChild(pre);
    }
    if (data.truncated) {
      const notice = document.createElement("div");
      notice.className = "preview-notice";
      notice.textContent = "文件较大，仅显示前半部分内容";
      body.appendChild(notice);
    }
    els.workspacePreviewBody.replaceChildren(body);
    return;
  }

  if (file.kind === "pdf") {
    const frame = document.createElement("iframe");
    frame.className = "workspace-pdf-preview";
    frame.src = workspaceFileUrl(file);
    frame.title = file.name;
    els.workspacePreviewBody.replaceChildren(frame);
    return;
  }

  const fallback = document.createElement("div");
  fallback.className = "workspace-file-fallback";
  fallback.textContent = "该文件类型暂不支持内嵌预览，可以新窗口打开或下载。";
  els.workspacePreviewBody.replaceChildren(fallback);
}

async function fetchWorkspaceFiles() {
  if (!els.workspaceFileList) return;
  const selectedId = state.selectedId;
  if (!selectedId) {
    state.workspaceFiles = [];
    state.workspaceLoading = false;
    state.workspaceError = "";
    renderWorkspaceFiles();
    return;
  }
  state.workspaceLoading = true;
  state.workspaceError = "";
  renderWorkspaceFiles();
  try {
    const res = await fetch(`/api/workspace/files?node_id=${encodeURIComponent(selectedId)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "读取 Workspace 失败");
    if (selectedId !== state.selectedId) return;
    state.workspaceFiles = data.files || [];
  } catch (err) {
    state.workspaceError = err.message;
  } finally {
    state.workspaceLoading = false;
    renderWorkspaceFiles();
  }
}

function handlePromptDragOver(event) {
  if (!Array.from(event.dataTransfer.types || []).includes("text/plain")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  els.promptInput.classList.add("drop-target");
}

function handlePromptDrop(event) {
  const rawRef = event.dataTransfer.getData("application/x-workspace-ref");
  if (!rawRef) return;
  event.preventDefault();
  els.promptInput.classList.remove("drop-target");
  try {
    const ref = JSON.parse(rawRef);
    if (!ref.path || state.workspaceRefs.some((item) => item.path === ref.path)) return;
    state.workspaceRefs.push(ref);
    renderComposer();
  } catch (err) {
    window.alert(err.message);
  }
}

function renderDirectionControls() {
  for (const button of els.directionBtns) {
    button.classList.toggle("active", button.dataset.direction === state.treeDirection);
  }
}

async function sendPrompt(event) {
  event.preventDefault();
  const prompt = els.promptInput.value.trim();
  if (!prompt || state.sending) return;
  state.sending = true;
  renderComposer();
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        parent_id: state.parentId,
        prompt,
        file_ids: [...state.fileIds],
        workspace_refs: state.workspaceRefs.map((item) => ({ path: item.path })),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "发送失败");
    state.selectedId = data.node.id;
    state.parentId = data.node.id;
    if (!data.node.parent_id) {
      state.selectedProjectId = data.node.id;
      localStorage.setItem("selectedProjectId", state.selectedProjectId);
    }
    state.fileIds.clear();
    state.workspaceRefs = [];
    els.promptInput.value = "";
    state.scrollConversationToLatest = true;
    await fetchTree();
    focusSelected();
    await fetchWorkspaceFiles();
  } catch (err) {
    window.alert(err.message);
  } finally {
    state.sending = false;
    renderComposer();
  }
}

async function uploadFiles() {
  const files = [...els.fileInput.files];
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  els.fileInput.value = "";
  const res = await fetch("/api/upload", { method: "POST", body: form });
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "上传失败");
    return;
  }
  for (const file of data.files || []) state.fileIds.add(file.id);
  await fetchTree();
}

async function saveNote() {
  const node = selectedNode();
  if (!node || state.noteSaving) return;
  state.noteSaving = true;
  renderNotePanel();
  try {
    const res = await fetch(`/api/nodes/${node.id}/note`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note_md: els.noteEditor.value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存失败");
    state.noteDirty = false;
    state.noteEditing = false;
    state.noteDraftNodeId = data.node.id;
    state.noteDraft = data.node.note_md || "";
    await fetchTree();
  } catch (err) {
    window.alert(err.message);
  } finally {
    state.noteSaving = false;
    renderNotePanel();
  }
}

async function readProjectNotes() {
  const projectId = state.selectedProjectId;
  if (!projectId || projectId === "__new__") return;
  const res = await fetch(`/api/projects/${projectId}/notes`);
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "读取失败");
    return;
  }
  state.projectNotes = data.notes || [];
  state.projectNotesProjectId = projectId;
  state.noteEditing = false;
  state.activeSideTab = "notes";
  renderSideTabs();
  renderNotePanel();
}

function cleanMarkdown(value) {
  return String(value || "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function markdownChildren(node) {
  return Array.from(node.childNodes || []).map((child) => markdownFromNode(child)).join("");
}

function markdownFromNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
  if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) return markdownChildren(node);
  if (node.nodeType !== Node.ELEMENT_NODE) return "";

  const el = node;
  const tag = el.tagName.toLowerCase();
  if (el.classList.contains("bubble-label") || el.classList.contains("bubble-meta")) return "";
  if (tag === "br") return "\n";
  if (tag === "hr") return "\n---\n";
  if (tag === "strong" || tag === "b") return `**${markdownChildren(el).trim()}**`;
  if (tag === "em" || tag === "i") return `*${markdownChildren(el).trim()}*`;
  if (tag === "code" && el.parentElement && el.parentElement.tagName.toLowerCase() !== "pre") {
    return `\`${el.textContent || ""}\``;
  }
  if (tag === "a") {
    const label = markdownChildren(el).trim() || el.textContent || "";
    const href = el.getAttribute("href") || "";
    return href ? `[${label}](${href})` : label;
  }
  if (tag === "pre") {
    const code = el.querySelector("code");
    const language = code && code.dataset.language ? code.dataset.language : "";
    const text = code ? code.textContent || "" : el.textContent || "";
    return code ? `\n\`\`\`${language}\n${text.replace(/\n$/, "")}\n\`\`\`\n` : `\n${text}\n`;
  }
  if (/^h[1-6]$/.test(tag)) {
    const level = Number(tag.slice(1));
    return `\n${"#".repeat(level)} ${markdownChildren(el).trim()}\n`;
  }
  if (tag === "p") return `\n${markdownChildren(el).trim()}\n`;
  if (tag === "blockquote") {
    const body = cleanMarkdown(markdownChildren(el));
    return `\n${body.split("\n").map((line) => `> ${line}`).join("\n")}\n`;
  }
  if (tag === "ul" || tag === "ol") {
    const items = Array.from(el.children).filter((child) => child.tagName.toLowerCase() === "li");
    return `\n${items.map((item, index) => {
      const marker = tag === "ol" ? `${index + 1}.` : "-";
      const body = cleanMarkdown(markdownChildren(item)).replace(/\n/g, "\n  ");
      return `${marker} ${body}`;
    }).join("\n")}\n`;
  }
  if (tag === "li") return markdownChildren(el);
  return markdownChildren(el);
}

function applyAncestorMarkdown(markdown, range) {
  let result = cleanMarkdown(markdown);
  if (!result) return "";
  let el = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentElement;
  while (el && el !== els.conversationList) {
    const tag = el.tagName.toLowerCase();
    if ((tag === "strong" || tag === "b") && !/^\*\*[\s\S]+\*\*$/.test(result)) {
      result = `**${result}**`;
    } else if ((tag === "em" || tag === "i") && !/^\*[\s\S]+\*$/.test(result)) {
      result = `*${result}*`;
    } else if (tag === "code" && el.parentElement && el.parentElement.tagName.toLowerCase() === "pre") {
      const language = el.dataset.language || "";
      result = `\`\`\`${language}\n${result}\n\`\`\``;
    } else if (tag === "code" && !/^`[\s\S]+`$/.test(result)) {
      result = `\`${result}\``;
    } else if (tag === "a" && !/^\[[\s\S]+\]\([^)]+\)$/.test(result)) {
      const href = el.getAttribute("href") || "";
      if (href) result = `[${result}](${href})`;
    } else if (/^h[1-6]$/.test(tag) && !/^#{1,6}\s/.test(result)) {
      result = `${"#".repeat(Number(tag.slice(1)))} ${result}`;
    } else if (tag === "li" && !/^(-|\d+\.)\s/.test(result)) {
      const parent = el.parentElement;
      const ordered = parent && parent.tagName.toLowerCase() === "ol";
      const siblings = parent ? Array.from(parent.children).filter((child) => child.tagName.toLowerCase() === "li") : [];
      const marker = ordered ? `${Math.max(1, siblings.indexOf(el) + 1)}.` : "-";
      result = `${marker} ${result.replace(/\n/g, "\n  ")}`;
    } else if (tag === "blockquote" && !/^>\s/m.test(result)) {
      result = result.split("\n").map((line) => `> ${line}`).join("\n");
    }
    el = el.parentElement;
  }
  return cleanMarkdown(result);
}

function selectedConversationMarkdown() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return "";
  const anchor = selection.anchorNode && (selection.anchorNode.nodeType === Node.ELEMENT_NODE
    ? selection.anchorNode
    : selection.anchorNode.parentElement);
  const focus = selection.focusNode && (selection.focusNode.nodeType === Node.ELEMENT_NODE
    ? selection.focusNode
    : selection.focusNode.parentElement);
  if (!anchor || !focus) return "";
  if (!els.conversationList.contains(anchor) || !els.conversationList.contains(focus)) return "";
  const parts = [];
  for (let i = 0; i < selection.rangeCount; i += 1) {
    const range = selection.getRangeAt(i);
    const ancestor = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
      ? range.commonAncestorContainer
      : range.commonAncestorContainer.parentElement;
    if (!ancestor || !els.conversationList.contains(ancestor)) continue;
    const markdown = applyAncestorMarkdown(markdownFromNode(range.cloneContents()), range);
    if (markdown) parts.push(markdown);
  }
  return cleanMarkdown(parts.join("\n\n")) || selection.toString().trim();
}

function showNoteContextMenu(event, text) {
  state.noteContextText = text;
  const menu = els.noteContextMenu;
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  const left = clamp(event.clientX, 8, window.innerWidth - rect.width - 8);
  const top = clamp(event.clientY, 8, window.innerHeight - rect.height - 8);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

function hideNoteContextMenu() {
  state.noteContextText = "";
  els.noteContextMenu.hidden = true;
}

async function appendSelectionToNote() {
  const node = selectedNode();
  const text = state.noteContextText.trim();
  hideNoteContextMenu();
  if (!node || !text) return;
  const res = await fetch(`/api/nodes/${node.id}/note/append`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "追加失败");
    return;
  }
  state.activeSideTab = "notes";
  state.noteDirty = false;
  state.noteDraftNodeId = data.node.id;
  state.noteDraft = data.node.note_md || "";
  await fetchTree();
}

async function patchSelected(payload) {
  const node = selectedNode();
  if (!node) return;
  const res = await fetch(`/api/nodes/${node.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const data = await res.json();
    window.alert(data.error || "更新失败");
    return;
  }
  await fetchTree();
}

async function deleteSelected() {
  const node = selectedNode();
  if (!node) return;
  const children = state.childrenByParent.get(node.id) || [];
  const confirmed = window.confirm(
    children.length
      ? "删除这个节点以及它后面的所有节点？"
      : "删除这个节点？"
  );
  if (!confirmed) return;
  const fallbackParentId = node.parent_id || null;
  const res = await fetch(`/api/nodes/${node.id}`, { method: "DELETE" });
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "删除失败");
    return;
  }
  if (fallbackParentId && state.nodeById.has(fallbackParentId)) {
    state.selectedId = fallbackParentId;
    state.parentId = fallbackParentId;
    state.selectedProjectId = rootIdForNode(fallbackParentId) || fallbackParentId;
  } else {
    state.selectedProjectId = "__new__";
    state.selectedId = null;
    state.parentId = null;
  }
  localStorage.setItem("selectedProjectId", state.selectedProjectId);
  await fetchTree();
  focusSelected();
  await fetchWorkspaceFiles();
}

function rootIdForNode(nodeId) {
  let current = state.nodeById.get(nodeId);
  const seen = new Set();
  while (current && current.parent_id && !seen.has(current.id)) {
    seen.add(current.id);
    const parent = state.nodeById.get(current.parent_id);
    if (!parent) break;
    current = parent;
  }
  return current ? current.id : null;
}

function exportSelected() {
  const node = selectedNode();
  if (!node) return;
  window.location.href = `/api/export/${node.id}`;
}

function maxSidePanelWidth() {
  return Math.max(MIN_SIDE_WIDTH, Math.min(MAX_SIDE_WIDTH, window.innerWidth - 280));
}

function applySidePanelWidth(width) {
  const safeWidth = clamp(Number(width) || DEFAULT_SIDE_WIDTH, MIN_SIDE_WIDTH, maxSidePanelWidth());
  state.sidePanelWidth = safeWidth;
  document.documentElement.style.setProperty("--side-panel-width", `${safeWidth}px`);
  renderMiniMap();
}

function initSidePanelWidth() {
  applySidePanelWidth(Number(localStorage.getItem(SIDE_WIDTH_KEY)) || DEFAULT_SIDE_WIDTH);
}

function startSidePanelResize(event) {
  if (!els.sideResizeHandle) return;
  event.preventDefault();
  state.resizingSidePanel = true;
  document.body.classList.add("resizing-side-panel");
}

function resizeSidePanel(event) {
  if (!state.resizingSidePanel) return;
  const nextWidth = window.innerWidth - event.clientX;
  applySidePanelWidth(nextWidth);
}

function stopSidePanelResize() {
  if (!state.resizingSidePanel) return;
  state.resizingSidePanel = false;
  document.body.classList.remove("resizing-side-panel");
  localStorage.setItem(SIDE_WIDTH_KEY, String(Math.round(state.sidePanelWidth)));
}

function startNewProject() {
  state.selectedProjectId = "__new__";
  state.parentId = null;
  state.selectedId = null;
  state.workspaceFiles = [];
  state.nodes = [];
  localStorage.setItem("selectedProjectId", state.selectedProjectId);
  rebuildIndexes();
  render();
  fetchWorkspaceFiles().catch(console.error);
  els.promptInput.focus();
}

function pollIfNeeded() {
  const active = state.allNodes.some((node) => node.status === "running" || node.status === "queued");
  if (active) {
    fetchTree().catch(console.error);
    fetchWorkspaceFiles().catch(console.error);
  }
}

function wireEvents() {
  els.composer.addEventListener("submit", sendPrompt);
  for (const button of els.sideTabBtns) {
    button.addEventListener("click", () => setSideTab(button.dataset.sideTab));
  }
  els.noteEditor.addEventListener("input", () => {
    const node = selectedNode();
    state.noteDraftNodeId = node ? node.id : null;
    state.noteDraft = els.noteEditor.value;
    state.noteDirty = Boolean(node);
    renderTree();
    renderNotePanel();
  });
  els.noteEditBtn.addEventListener("click", () => {
    if (!selectedNode()) return;
    state.noteEditing = true;
    renderNotePanel();
    els.noteEditor.focus();
  });
  els.noteSaveBtn.addEventListener("click", saveNote);
  els.noteProjectBtn.addEventListener("click", readProjectNotes);
  els.conversationList.addEventListener("contextmenu", (event) => {
    const text = selectedConversationMarkdown();
    if (!text || !selectedNode()) return;
    event.preventDefault();
    showNoteContextMenu(event, text);
  });
  els.noteContextAppendBtn.addEventListener("click", appendSelectionToNote);
  document.addEventListener("click", (event) => {
    if (!els.noteContextMenu.hidden && !els.noteContextMenu.contains(event.target)) {
      hideNoteContextMenu();
    }
  });
  els.sideResizeHandle.addEventListener("pointerdown", startSidePanelResize);
  window.addEventListener("pointermove", resizeSidePanel);
  window.addEventListener("pointerup", stopSidePanelResize);
  els.workspaceToggleBtn.addEventListener("click", () => setWorkspaceDrawerOpen(!state.workspaceOpen));
  els.workspaceCollapseBtn.addEventListener("click", () => setWorkspaceDrawerOpen(false));
  els.workspaceRefreshBtn.addEventListener("click", () => fetchWorkspaceFiles().catch(console.error));
  els.workspacePreviewBackdrop.addEventListener("click", closeWorkspacePreview);
  els.workspacePreviewCloseBtn.addEventListener("click", closeWorkspacePreview);
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.workspacePreview.hidden) closeWorkspacePreview();
    if (event.key === "Escape") {
      hideNoteContextMenu();
      hideNodeNotePreview();
      hideNodePromptPreview();
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s" && state.activeSideTab === "notes") {
      event.preventDefault();
      saveNote();
    }
  });
  els.promptInput.addEventListener("dragover", handlePromptDragOver);
  els.promptInput.addEventListener("dragleave", () => els.promptInput.classList.remove("drop-target"));
  els.promptInput.addEventListener("drop", handlePromptDrop);
  els.promptInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (!state.sending && els.promptInput.value.trim()) {
      els.composer.requestSubmit();
    }
  });
  els.fileInput.addEventListener("change", uploadFiles);
  els.searchInput.addEventListener("input", () => {
    state.search = els.searchInput.value;
    renderTree();
  });
  for (const button of els.directionBtns) {
    button.addEventListener("click", () => {
      const direction = button.dataset.direction === "right" ? "right" : "down";
      if (direction === state.treeDirection) return;
      state.treeDirection = direction;
      localStorage.setItem("treeDirection", direction);
      render();
      focusSelected();
    });
  }
  els.projectSelect.addEventListener("change", () => {
    const value = els.projectSelect.value;
    if (value === "__new__") {
      startNewProject();
      return;
    }
    state.selectedProjectId = value;
    state.nodes = nodesForProject(value);
    state.selectedId = value;
    state.parentId = value;
    localStorage.setItem("selectedProjectId", value);
    rebuildIndexes();
    render();
    focusSelected();
    fetchWorkspaceFiles().catch(console.error);
  });
  els.emptyState.addEventListener("click", () => {
    els.promptInput.focus();
  });
  els.refreshBtn.addEventListener("click", () => {
    fetchTree().catch(console.error);
    fetchWorkspaceFiles().catch(console.error);
  });
  els.focusBtn.addEventListener("click", focusSelected);
  els.zoomInBtn.addEventListener("click", () => {
    const rect = els.canvasHost.getBoundingClientRect();
    zoomAt(1.16, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  els.zoomOutBtn.addEventListener("click", () => {
    const rect = els.canvasHost.getBoundingClientRect();
    zoomAt(0.86, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  els.deleteBtn.addEventListener("click", deleteSelected);
  els.exportBtn.addEventListener("click", exportSelected);

  els.treeSvg.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 1.08 : 0.92, event.clientX, event.clientY);
  }, { passive: false });
  els.treeSvg.addEventListener("pointerdown", (event) => {
    if (event.target.closest && event.target.closest(".node-card")) return;
    state.dragging = true;
    state.dragStart = {
      x: event.clientX,
      y: event.clientY,
      tx: state.transform.x,
      ty: state.transform.y,
    };
    els.treeSvg.setPointerCapture(event.pointerId);
  });
  els.treeSvg.addEventListener("pointermove", (event) => {
    if (!state.dragging || !state.dragStart) return;
    state.transform.x = state.dragStart.tx + event.clientX - state.dragStart.x;
    state.transform.y = state.dragStart.ty + event.clientY - state.dragStart.y;
    applyTransform();
    renderMiniMap();
  });
  els.treeSvg.addEventListener("pointerup", (event) => {
    state.dragging = false;
    state.dragStart = null;
    try {
      els.treeSvg.releasePointerCapture(event.pointerId);
    } catch {
      // Pointer capture may already be released by the browser.
    }
  });
  window.addEventListener("resize", () => {
    applySidePanelWidth(state.sidePanelWidth);
    renderMiniMap();
  });
}

initSidePanelWidth();
wireEvents();
fetchTree({ keepSelection: false })
  .then(() => {
    if (state.nodes.length) focusSelected();
    return fetchWorkspaceFiles();
  })
  .catch((err) => {
    window.alert(err.message);
  });
setInterval(pollIfNeeded, 2500);
