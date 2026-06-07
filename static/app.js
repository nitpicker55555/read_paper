const NODE_W = 238;
const NODE_H = 112;
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
  workDir: "",
  selectedProjectId: localStorage.getItem("selectedProjectId") || null,
  selectedId: null,
  parentId: null,
  fileIds: new Set(),
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
  conversationList: document.getElementById("conversationList"),
  deleteBtn: document.getElementById("deleteBtn"),
  exportBtn: document.getElementById("exportBtn"),
  fileInput: document.getElementById("fileInput"),
  fileList: document.getElementById("fileList"),
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
        try {
          const url = new URL(href, window.location.origin);
          if (["http:", "https:", "mailto:"].includes(url.protocol)) safeUrl = url.href;
        } catch {
          safeUrl = null;
        }
        if (!safeUrl) {
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
  renderComposer();
  renderFiles();
  renderProjectSelect();
  renderDirectionControls();
  els.workspaceLabel.textContent = state.workDir ? state.workDir : "";
  els.emptyState.classList.toggle("visible", state.nodes.length === 0);
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
    foreign.setAttribute("x", pos.x);
    foreign.setAttribute("y", pos.y);
    foreign.setAttribute("width", NODE_W);
    foreign.setAttribute("height", NODE_H);
    foreign.appendChild(makeNodeCard(node));
    els.nodeLayer.appendChild(foreign);
  }
}

function edgeClass(parent, node) {
  const active = state.selectedPath.has(parent.id) && state.selectedPath.has(node.id);
  const dimmed = state.selectedId && !active;
  return ["edge", active ? "active" : "", dimmed ? "dimmed" : ""].filter(Boolean).join(" ");
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

  const top = document.createElement("div");
  top.className = "node-top";
  const title = document.createElement("div");
  title.className = "node-title";
  title.textContent = node.title || "节点";
  const dot = document.createElement("span");
  dot.className = `status-dot status-${node.status || "queued"}`;
  top.append(title, dot);

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
  card.append(top, snippet, bottom);

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
  state.selectedId = id;
  state.parentId = id;
  render();
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

function fileById(fileId) {
  return state.files.find((item) => item.id === fileId) || null;
}

function contextAttachmentFiles() {
  const attachments = [];
  const seen = new Set();
  for (const node of selectedConversationPath()) {
    for (const attachment of node.attachments || []) {
      const fileId = String(attachment.id || "").trim();
      if (!fileId || seen.has(fileId)) continue;
      seen.add(fileId);
      const file = fileById(fileId);
      const size = Number(attachment.size ?? (file && file.size));
      attachments.push({
        id: fileId,
        original_name: attachment.name || (file && file.original_name) || "未命名附件",
        mime: attachment.mime || (file && file.mime) || "",
        size: Number.isFinite(size) ? size : 0,
        source_title: node.title || "节点",
      });
    }
  }
  return attachments;
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

function makeBubble(role, text, meta = "", tools = [], active = false) {
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
    bubbles.push(makeBubble("user", item.prompt, item.created_at || ""));
    const active = isNodeActive(item);
    const agentText = item.answer || item.error || (active ? "正在处理" : item.status === "done" ? "" : statusText(item.status));
    bubbles.push(makeBubble("agent", agentText, item.completed_at || item.updated_at || "", toolCallsForNode(item), active));
  }
  els.conversationList.replaceChildren(...bubbles);
  els.conversationList.scrollTop = els.conversationList.scrollHeight;
}

function renderComposer() {
  els.selectedFiles.replaceChildren();
  for (const fileId of state.fileIds) {
    const file = state.files.find((item) => item.id === fileId);
    if (!file) continue;
    const chip = document.createElement("span");
    chip.className = "file-chip";
    const name = document.createElement("span");
    name.textContent = file.original_name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.fileIds.delete(fileId);
      renderComposer();
      renderFiles();
    });
    chip.append(name, remove);
    els.selectedFiles.appendChild(chip);
  }
  els.sendBtn.disabled = state.sending;
}

function renderFiles() {
  const files = contextAttachmentFiles();
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "soft-label";
    empty.textContent = "暂无附件";
    els.fileList.replaceChildren(empty);
    return;
  }
  const rows = files.map((file) => {
    const row = document.createElement("div");
    row.className = "file-row";
    const main = document.createElement("div");
    main.className = "file-main";
    const name = document.createElement("div");
    name.className = "file-name";
    name.textContent = file.original_name;
    const sub = document.createElement("div");
    sub.className = "file-sub";
    sub.textContent = [`来自 ${compact(file.source_title, 28)}`, formatBytes(file.size), file.mime || "unknown"].join(" · ");
    main.append(name, sub);
    row.appendChild(main);
    return row;
  });
  els.fileList.replaceChildren(...rows);
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
    els.promptInput.value = "";
    await fetchTree();
    focusSelected();
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
  state.nodes = [];
  localStorage.setItem("selectedProjectId", state.selectedProjectId);
  rebuildIndexes();
  render();
  els.promptInput.focus();
}

function pollIfNeeded() {
  const active = state.allNodes.some((node) => node.status === "running" || node.status === "queued");
  if (active) fetchTree().catch(console.error);
}

function wireEvents() {
  els.composer.addEventListener("submit", sendPrompt);
  els.sideResizeHandle.addEventListener("pointerdown", startSidePanelResize);
  window.addEventListener("pointermove", resizeSidePanel);
  window.addEventListener("pointerup", stopSidePanelResize);
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
  });
  els.emptyState.addEventListener("click", () => {
    els.promptInput.focus();
  });
  els.refreshBtn.addEventListener("click", () => fetchTree().catch(console.error));
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
  })
  .catch((err) => {
    window.alert(err.message);
  });
setInterval(pollIfNeeded, 2500);
