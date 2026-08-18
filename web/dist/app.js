const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  rules: [], handlers: [], selectedRules: new Set(), jobs: [],
  path: "", query: "", offset: 0, hasMore: false,
  directories: [], files: [], selected: new Set(), focusedId: null,
  history: [""], historyIndex: 0, treeCache: new Map(), expanded: new Set([""]),
  batchDownloadUrl: "", previewId: null, searchTimer: null, toastTimer: null,
};

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}
function fmtBytes(value) {
  let size = Number(value) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${index ? size.toFixed(size >= 10 ? 1 : 2) : Math.round(size)} ${units[index]}`;
}
function fmtTime(value) {
  if (!value) return "—";
  return new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12: false });
}
function isImage(name) { return /\.(jpe?g|png|gif|webp|bmp|tiff?)$/i.test(name || ""); }
function previewUrl(id) { return `/api/v1/asset/preview?id=${encodeURIComponent(id)}`; }
function downloadUrl(id) { return `/api/v1/asset/download?id=${encodeURIComponent(id)}`; }

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail; } catch (_) {}
    const error = new Error(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail));
    error.detail = detail; error.status = response.status; throw error;
  }
  return (response.headers.get("content-type") || "").includes("application/json")
    ? response.json() : response;
}
function toast(message) {
  clearTimeout(state.toastTimer); const el = $("#toast"); el.textContent = message; el.hidden = false;
  state.toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}
function showMessage(title, body) {
  $("#message-title").textContent = title;
  $("#message-body").innerHTML = body;
  $("#modal-backdrop").hidden = false; $("#message-modal").hidden = false;
}
function closeMessage() { $("#message-modal").hidden = true; if ($("#rule-modal").hidden) $("#modal-backdrop").hidden = true; }

function switchTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $("#view-pack").hidden = name !== "pack"; $("#view-library").hidden = name !== "library";
  if (name === "library" && !state.treeCache.size) loadLibrary("").catch(showError);
}

async function refreshStatus() {
  const status = await api("/api/v1/status");
  $("#meta-count").textContent = `素材 ${(status.asset_count || 0).toLocaleString()}`;
  const ready = status.ready_for_pack || status.asset_count > 0;
  const chip = $("#meta-ready"); chip.textContent = ready ? (status.sync_complete ? "已就绪" : "同步中，可用") : "准备中";
  chip.className = `status-dot ${ready ? "ok" : "warn"}`;
}

/* rules */
async function loadRules({ preserve = false } = {}) {
  const data = await api("/api/v1/pack-rules");
  const before = new Set(state.selectedRules); state.rules = data.rules || []; state.handlers = data.handlers || [];
  state.selectedRules = new Set(
    state.rules.filter((rule) => rule.enabled && (!preserve || before.has(rule.id))).map((rule) => rule.id)
  );
  if (preserve && before.size === 0) state.selectedRules.clear();
  renderRules();
}
function renderRules() {
  const list = $("#rules-list");
  if (!state.rules.length) { list.innerHTML = '<div class="empty">暂无规则，可点击“添加规则”创建。</div>'; }
  else list.innerHTML = state.rules.map((rule) => `
    <div class="rule-row ${rule.enabled ? "" : "disabled"}" data-rule="${escapeHtml(rule.id)}">
      <input class="rule-check" type="checkbox" ${state.selectedRules.has(rule.id) ? "checked" : ""} ${rule.enabled ? "" : "disabled"} aria-label="选择 ${escapeHtml(rule.name)}" />
      <div class="rule-copy"><strong>${escapeHtml(rule.name)}</strong><p>${escapeHtml(rule.description)}</p></div>
      <div class="rule-actions"><button class="edit-rule" type="button">编辑</button><button class="delete delete-rule" type="button">删除</button></div>
    </div>`).join("");
  $$(".rule-row", list).forEach((row) => {
    const id = row.dataset.rule;
    $(".rule-check", row)?.addEventListener("change", (event) => {
      event.target.checked ? state.selectedRules.add(id) : state.selectedRules.delete(id); updateRuleCount();
    });
    $(".edit-rule", row)?.addEventListener("click", () => openRuleModal(state.rules.find((rule) => rule.id === id)));
    $(".delete-rule", row)?.addEventListener("click", async () => {
      const rule = state.rules.find((item) => item.id === id);
      if (!confirm(`确定删除规则“${rule?.name || id}”吗？已创建任务中的规则快照不受影响。`)) return;
      await api(`/api/v1/pack-rules/${encodeURIComponent(id)}`, { method: "DELETE" });
      state.selectedRules.delete(id); await loadRules({ preserve: true }); toast("规则已删除");
    });
  });
  updateRuleCount();
}
function updateRuleCount() { $("#selected-rule-count").textContent = String(state.selectedRules.size); }
function openRuleModal(rule = null) {
  $("#rule-modal-title").textContent = rule ? "编辑规则" : "添加规则";
  $("#rule-id").value = rule?.id || ""; $("#rule-name").value = rule?.name || "";
  $("#rule-description").value = rule?.description || ""; $("#rule-enabled").checked = rule?.enabled ?? true;
  $("#rule-handler").innerHTML = state.handlers.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  $("#rule-handler").value = rule?.handler || state.handlers[0]?.value || "note";
  $("#modal-backdrop").hidden = false; $("#rule-modal").hidden = false;
}
function closeRuleModal() { $("#rule-modal").hidden = true; if ($("#message-modal").hidden) $("#modal-backdrop").hidden = true; }
async function submitRule(event) {
  event.preventDefault(); const id = $("#rule-id").value;
  const payload = { name: $("#rule-name").value.trim(), description: $("#rule-description").value.trim(), handler: $("#rule-handler").value, enabled: $("#rule-enabled").checked, sort_order: id ? state.rules.find((rule) => rule.id === id)?.sort_order || 1000 : 1000, config: {} };
  await api(id ? `/api/v1/pack-rules/${encodeURIComponent(id)}` : "/api/v1/pack-rules", { method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  closeRuleModal(); await loadRules({ preserve: true }); toast(id ? "规则已更新" : "规则已添加");
}

/* jobs */
async function loadJobs() { const data = await api("/api/v1/jobs?limit=20"); state.jobs = data.jobs || []; renderJobs(); }
function renderJobs() {
  const box = $("#jobs-list");
  if (!state.jobs.length) { box.innerHTML = '<div class="empty">暂无任务</div>'; return; }
  box.innerHTML = state.jobs.map((job) => {
    const percent = Number(job.progress?.percent) || 0;
    const label = { queued: "排队中", running: "处理中", done: "已完成", failed: "失败" }[job.status] || job.status;
    return `<div class="job-row"><div><strong>${escapeHtml(job.filename || job.id)}</strong><small>${escapeHtml(label)} · ${escapeHtml(job.progress?.label || "")}</small></div><div class="job-progress"><i style="width:${Math.max(0, Math.min(100, percent))}%"></i></div><div>${job.has_download ? `<a class="btn secondary" href="/api/v1/jobs/${job.id}/download">下载</a>` : `<small>${percent}%</small>`}</div></div>`;
  }).join("");
}
async function submitPack(event) {
  event.preventDefault(); const file = $("#excel").files[0]; if (!file) return;
  const form = new FormData(); form.append("file", file); form.append("super_dir_name", $("#super").value.trim()); form.append("rule_ids", JSON.stringify([...state.selectedRules]));
  $("#pack-submit").disabled = true; $("#pack-submit").textContent = "正在提交…"; $("#pack-msg").hidden = true;
  try { const result = await api("/api/v1/jobs", { method: "POST", body: form }); $("#excel").value = ""; $("#excel-label").textContent = "拖入订单 Excel，或点击选择"; await loadJobs(); const duplicate = result.duplicate_rows ? `，其中 ${result.duplicate_rows} 个重复行按次数分别输出` : ""; toast(`已收到 ${result.input_rows} 行、${result.unique_rows} 个唯一编码${duplicate}`); }
  catch (error) { $("#pack-msg").hidden = false; $("#pack-msg").className = "form-message err"; $("#pack-msg").textContent = error.message; }
  finally { $("#pack-submit").disabled = false; $("#pack-submit").textContent = "提交打包"; }
}

/* virtual directory tree */
async function fetchTree(path, limit = 200, offset = 0) {
  const params = new URLSearchParams({ path, q: state.query, limit: String(limit), offset: String(offset) });
  return api(`/api/v1/library/tree?${params}`);
}
async function ensureTreePath(path) {
  const parts = path ? path.split("/") : []; let current = ""; state.expanded.add("");
  if (!state.treeCache.has("")) { const root = await fetchTree("", 1); state.treeCache.set("", root.directories || []); }
  for (const part of parts) { current = current ? `${current}/${part}` : part; state.expanded.add(current); if (!state.treeCache.has(current)) { const data = await fetchTree(current, 1); state.treeCache.set(current, data.directories || []); } }
}
function renderTree() {
  const renderNode = (name, path, depth) => {
    const expanded = state.expanded.has(path); const children = state.treeCache.get(path) || [];
    return `<div><div class="tree-row ${state.path === path ? "active" : ""}" data-tree-path="${escapeHtml(path)}" style="padding-left:${7 + depth * 4}px"><span class="twisty">${children.length ? (expanded ? "▾" : "▸") : ""}</span><span class="folder-icon">▰</span><span class="tree-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span></div>${expanded && children.length ? `<div class="tree-children">${children.map((child) => renderNode(child.name, child.path, depth + 1)).join("")}</div>` : ""}</div>`;
  };
  $("#directory-tree").innerHTML = renderNode("素材库", "", 0);
  $$(".tree-row").forEach((row) => row.addEventListener("click", async (event) => {
    const path = row.dataset.treePath; const clickedTwisty = event.target.classList.contains("twisty");
    if (clickedTwisty) { if (state.expanded.has(path)) state.expanded.delete(path); else { state.expanded.add(path); if (!state.treeCache.has(path)) { const data = await fetchTree(path, 1); state.treeCache.set(path, data.directories || []); } } renderTree(); }
    else navigate(path);
  }));
}
async function loadLibrary(path, { append = false, pushHistory = false } = {}) {
  const clean = path.replace(/^\/+|\/+$/g, "");
  const offset = append ? state.files.length : 0; state.path = clean;
  if (!append) { state.offset = 0; state.selected.clear(); state.focusedId = null; state.batchDownloadUrl = ""; }
  const data = await fetchTree(clean, 200, offset);
  state.directories = data.directories || []; state.files = append ? [...state.files, ...(data.files || [])] : (data.files || []);
  state.hasMore = Boolean(data.has_more); if (!state.query) state.treeCache.set(clean, state.directories);
  await ensureTreePath(clean); if (pushHistory && state.history[state.historyIndex] !== clean) { state.history = state.history.slice(0, state.historyIndex + 1); state.history.push(clean); state.historyIndex += 1; }
  renderBreadcrumbs(data.breadcrumbs || []); renderTree(); renderLibrary(); updateSelection();
}
function navigate(path) { loadLibrary(path, { pushHistory: true }).catch(showError); }
function renderBreadcrumbs(items) { $("#breadcrumbs").innerHTML = items.map((item) => `<button type="button" data-path="${escapeHtml(item.path)}">${escapeHtml(item.name)}</button>`).join(""); $$("button", $("#breadcrumbs")).forEach((button) => button.addEventListener("click", () => navigate(button.dataset.path))); }
function renderLibrary() {
  $("#folder-title").textContent = state.query ? `搜索：${state.query}` : (state.path.split("/").pop() || "素材库");
  $("#folder-count").textContent = `${state.directories.length} 个目录 · ${state.files.length}${state.hasMore ? "+" : ""} 个文件`;
  $("#folder-grid").innerHTML = state.directories.map((folder) => `<div class="folder-tile" data-path="${escapeHtml(folder.path)}"><span class="folder-art">▰</span><span><strong title="${escapeHtml(folder.name)}">${escapeHtml(folder.name)}</strong><small>${folder.file_count || 0} 个文件</small></span></div>`).join("");
  $$(".folder-tile").forEach((tile) => { tile.addEventListener("dblclick", () => navigate(tile.dataset.path)); tile.addEventListener("click", () => { $$(".folder-tile").forEach((item) => item.classList.remove("selected")); tile.classList.add("selected"); }); });
  $("#file-grid").innerHTML = state.files.map((file) => {
    const preview = file.previewable || isImage(file.file_name); const ext = (file.file_name.split(".").pop() || "FILE").slice(0, 5).toUpperCase();
    return `<article class="file-tile ${state.selected.has(file.asset_id) ? "selected" : ""}" draggable="true" data-id="${escapeHtml(file.asset_id)}"><span class="file-check">✓</span><div class="file-thumb">${preview ? `<img src="${previewUrl(file.asset_id)}" alt="" loading="lazy" />` : `<span class="file-icon">${escapeHtml(ext)}</span>`}</div><div class="file-label"><strong title="${escapeHtml(file.file_name)}">${escapeHtml(file.file_name)}</strong><small>${fmtBytes(file.file_size)}</small></div></article>`;
  }).join("");
  bindFileTiles(); $("#load-more").hidden = !state.hasMore; $("#library-empty").hidden = Boolean(state.directories.length || state.files.length);
}
function bindFileTiles() {
  $$(".file-tile").forEach((tile) => {
    tile.addEventListener("click", (event) => selectFile(tile.dataset.id, event.ctrlKey || event.metaKey || event.shiftKey));
    tile.addEventListener("dblclick", () => { const file = state.files.find((item) => item.asset_id === tile.dataset.id); if (file?.previewable || isImage(file?.file_name)) openPreview(file.asset_id); else window.location.href = downloadUrl(file.asset_id); });
    tile.addEventListener("dragstart", (event) => startDownloadDrag(event, tile.dataset.id));
  });
}
function selectFile(id, additive = false) { if (!additive) state.selected.clear(); if (additive && state.selected.has(id)) state.selected.delete(id); else state.selected.add(id); state.focusedId = id; renderLibrary(); updateSelection(); }
function focusedFile() { return state.files.find((file) => file.asset_id === state.focusedId) || state.files.find((file) => state.selected.has(file.asset_id)); }
function updateSelection() {
  const count = state.selected.size; $("#selection-count").textContent = count ? `已选择 ${count} 项` : "未选择"; $("#clear-selection").hidden = !count; $("#download-selected").disabled = !count;
  const file = focusedFile(); $("#detail-empty").hidden = Boolean(file); $("#file-detail").hidden = !file;
  $(".detail-pane").classList.toggle("open", Boolean(file));
  if (file) renderDetail(file); prepareBatchTicket();
}
function renderDetail(file) {
  const previewable = file.previewable || isImage(file.file_name); const image = $("#detail-image"); image.hidden = !previewable; $("#detail-placeholder").hidden = previewable;
  if (previewable) image.src = previewUrl(file.asset_id); else $("#detail-placeholder").textContent = (file.file_name.split(".").pop() || "FILE").toUpperCase();
  $("#detail-name").textContent = file.file_name; $("#detail-size").textContent = fmtBytes(file.file_size); $("#detail-time").textContent = fmtTime(file.updated_at); $("#detail-path").textContent = `/${file.virtual_path || file.file_name}`; $("#detail-sku").textContent = file.sku_code || "—"; $("#detail-dedup").textContent = file.deduplicated ? "已按文件名复用" : "唯一文件名"; $("#open-large-preview").hidden = !previewable; $("#single-download").href = downloadUrl(file.asset_id);
}
async function prepareBatchTicket() { const ids = [...state.selected]; state.batchDownloadUrl = ""; if (ids.length < 2) return; try { const data = await api("/api/v1/assets/download-ticket", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }) }); if (ids.every((id) => state.selected.has(id)) && ids.length === state.selected.size) state.batchDownloadUrl = new URL(data.download_url, location.href).href; } catch (_) {} }
function startDownloadDrag(event, id) { if (!state.selected.has(id)) { state.selected = new Set([id]); state.focusedId = id; updateSelection(); } const ids = [...state.selected]; const file = state.files.find((item) => item.asset_id === id); const url = ids.length > 1 ? state.batchDownloadUrl : new URL(downloadUrl(id), location.href).href; if (!url) { event.preventDefault(); toast("批量下载正在准备，请稍后再拖一次"); return; } const name = ids.length > 1 ? `素材下载_${ids.length}项.zip` : file.file_name; event.dataTransfer.effectAllowed = "copy"; event.dataTransfer.setData("DownloadURL", `application/octet-stream:${name}:${url}`); event.dataTransfer.setData("text/uri-list", url); }
async function downloadSelected() {
  const ids = [...state.selected]; if (!ids.length) return;
  let url = ids.length === 1 ? downloadUrl(ids[0]) : state.batchDownloadUrl;
  if (ids.length > 1 && !url) {
    const ticket = await api("/api/v1/assets/download-ticket", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
    });
    url = ticket.download_url;
  }
  const anchor = document.createElement("a"); anchor.href = url;
  anchor.download = ids.length > 1 ? "素材下载.zip" : focusedFile()?.file_name || "素材下载";
  document.body.appendChild(anchor); anchor.click(); anchor.remove();
}

/* rectangle select */
let dragSelect = null;
function beginRectangle(event) { if (event.button !== 0 || event.target.closest(".file-tile,.folder-tile,button,a")) return; const canvas = $("#file-canvas"); dragSelect = { x: event.clientX, y: event.clientY, additive: event.ctrlKey || event.metaKey, original: new Set(state.selected) }; $("#selection-box").hidden = false; canvas.setPointerCapture?.(event.pointerId); event.preventDefault(); }
function moveRectangle(event) { if (!dragSelect) return; const left = Math.min(dragSelect.x, event.clientX), top = Math.min(dragSelect.y, event.clientY), right = Math.max(dragSelect.x, event.clientX), bottom = Math.max(dragSelect.y, event.clientY); const box = $("#selection-box"); Object.assign(box.style, { left: `${left}px`, top: `${top}px`, width: `${right-left}px`, height: `${bottom-top}px` }); const selected = dragSelect.additive ? new Set(dragSelect.original) : new Set(); $$(".file-tile").forEach((tile) => { const rect = tile.getBoundingClientRect(); if (rect.right >= left && rect.left <= right && rect.bottom >= top && rect.top <= bottom) selected.add(tile.dataset.id); }); state.selected = selected; $$(".file-tile").forEach((tile) => tile.classList.toggle("selected", state.selected.has(tile.dataset.id))); $("#selection-count").textContent = state.selected.size ? `已选择 ${state.selected.size} 项` : "未选择"; }
function endRectangle() { if (!dragSelect) return; dragSelect = null; $("#selection-box").hidden = true; state.focusedId = [...state.selected].at(-1) || null; updateSelection(); }

/* upload */
async function entriesFromDrop(dataTransfer) {
  const results = [];
  async function walk(entry, prefix = "") {
    if (results.length >= 200) return;
    if (entry.isFile) await new Promise((resolve) => entry.file((file) => { results.push({ file, relative: `${prefix}${file.name}` }); resolve(); }, resolve));
    else if (entry.isDirectory) { const reader = entry.createReader(); let batch; do { batch = await new Promise((resolve) => reader.readEntries(resolve)); for (const child of batch) await walk(child, `${prefix}${entry.name}/`); } while (batch.length); }
  }
  const items = [...(dataTransfer.items || [])];
  if (items.some((item) => item.webkitGetAsEntry)) { for (const item of items) { const entry = item.webkitGetAsEntry?.(); if (entry) await walk(entry); } }
  else [...dataTransfer.files].forEach((file) => results.push({ file, relative: file.name }));
  return results;
}
async function uploadItems(items) {
  if (!items.length) return; $("#upload-banner").hidden = false; $("#upload-banner").textContent = `正在添加 ${items.length} 个文件…`;
  const form = new FormData(); items.forEach((item) => form.append("files", item.file, item.file.name)); form.append("target_path", state.path); form.append("relative_paths", JSON.stringify(items.map((item) => item.relative || item.file.name)));
  try { const result = await api("/api/v1/library/upload", { method: "POST", body: form }); toast(`已添加 ${result.added} 个资源`); await loadLibrary(state.path); await refreshStatus(); }
  catch (error) { if (error.status === 409 && error.detail?.duplicates) { showMessage("文件名重复", `<p>以下文件名已存在，整批未添加：</p><ul>${error.detail.duplicates.map((item) => `<li>${escapeHtml(item.file_name)}</li>`).join("")}</ul><p>请重命名后再次拖入。</p>`); } else showError(error); }
  finally { $("#upload-banner").hidden = true; }
}

/* preview */
function previewFiles() { return state.files.filter((file) => file.previewable || isImage(file.file_name)); }
function openPreview(id) { const files = previewFiles(), file = files.find((item) => item.asset_id === id); if (!file) return; state.previewId = id; $("#preview-title").textContent = file.file_name; $("#preview-sub").textContent = `${fmtBytes(file.file_size)} · /${file.virtual_path}`; $("#preview-image").src = previewUrl(id); $("#preview-image").classList.remove("zoomed"); $("#preview-modal").hidden = false; }
function previewStep(delta) { const files = previewFiles(); if (!files.length) return; let index = files.findIndex((item) => item.asset_id === state.previewId); index = (index + delta + files.length) % files.length; openPreview(files[index].asset_id); }
function closePreview() { $("#preview-modal").hidden = true; $("#preview-image").removeAttribute("src"); }

function showError(error) { console.error(error); showMessage("操作失败", `<p>${escapeHtml(error?.message || String(error))}</p>`); }

/* events */
$$(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
$("#btn-refresh").addEventListener("click", () => Promise.all([refreshStatus(), loadJobs(), loadRules({ preserve: true }), !$("#view-library").hidden ? loadLibrary(state.path) : Promise.resolve()]).catch(showError));
$("#add-rule").addEventListener("click", () => openRuleModal()); $("#rule-form").addEventListener("submit", (event) => submitRule(event).catch(showError)); $$('[data-close-modal]').forEach((button) => button.addEventListener("click", closeRuleModal)); $$('[data-close-message]').forEach((button) => button.addEventListener("click", closeMessage));
$("#select-all-rules").addEventListener("click", () => { const enabled = state.rules.filter((rule) => rule.enabled); const all = enabled.every((rule) => state.selectedRules.has(rule.id)); state.selectedRules = new Set(all ? [] : enabled.map((rule) => rule.id)); renderRules(); });
$("#pack-form").addEventListener("submit", submitPack); $("#refresh-jobs").addEventListener("click", () => loadJobs().catch(showError));
$("#excel").addEventListener("change", (event) => { $("#excel-label").textContent = event.target.files[0]?.name || "拖入订单 Excel，或点击选择"; });
const excelDrop = $("#excel-drop"); ["dragenter", "dragover"].forEach((name) => excelDrop.addEventListener(name, (event) => { event.preventDefault(); excelDrop.classList.add("dragging"); })); ["dragleave", "drop"].forEach((name) => excelDrop.addEventListener(name, () => excelDrop.classList.remove("dragging")));
$("#nav-home").addEventListener("click", () => navigate("")); $("#nav-up").addEventListener("click", () => navigate(state.path.split("/").slice(0,-1).join("/"))); $("#nav-back").addEventListener("click", () => { if (state.historyIndex <= 0) return; state.historyIndex -= 1; loadLibrary(state.history[state.historyIndex]).catch(showError); });
const librarySearch = $("#library-search");
function submitLibrarySearch() { clearTimeout(state.searchTimer); state.query = librarySearch.value.trim(); loadLibrary(state.path).catch(showError); }
librarySearch.addEventListener("input", () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(submitLibrarySearch, 250); });
librarySearch.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); submitLibrarySearch(); } });
librarySearch.addEventListener("search", submitLibrarySearch);
$("#upload-files").addEventListener("click", () => $("#file-picker").click()); $("#upload-folder").addEventListener("click", () => $("#folder-picker").click());
$("#file-picker").addEventListener("change", (event) => uploadItems([...event.target.files].map((file) => ({ file, relative: file.name }))).finally(() => { event.target.value = ""; })); $("#folder-picker").addEventListener("change", (event) => uploadItems([...event.target.files].map((file) => ({ file, relative: file.webkitRelativePath || file.name }))).finally(() => { event.target.value = ""; }));
const canvas = $("#file-canvas"); canvas.addEventListener("pointerdown", beginRectangle); canvas.addEventListener("pointermove", moveRectangle); canvas.addEventListener("pointerup", endRectangle); canvas.addEventListener("pointercancel", endRectangle);
let dragDepth = 0; canvas.addEventListener("dragenter", (event) => { if ([...event.dataTransfer.types].includes("Files")) { event.preventDefault(); dragDepth += 1; $("#drop-zone").hidden = false; } }); canvas.addEventListener("dragover", (event) => { if ([...event.dataTransfer.types].includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } }); canvas.addEventListener("dragleave", () => { dragDepth -= 1; if (dragDepth <= 0) { dragDepth = 0; $("#drop-zone").hidden = true; } }); canvas.addEventListener("drop", async (event) => { event.preventDefault(); dragDepth = 0; $("#drop-zone").hidden = true; uploadItems(await entriesFromDrop(event.dataTransfer)); });
$("#download-selected").addEventListener("click", () => downloadSelected().catch(showError)); $("#clear-selection").addEventListener("click", () => { state.selected.clear(); state.focusedId = null; renderLibrary(); updateSelection(); }); $("#load-more").addEventListener("click", () => loadLibrary(state.path, { append: true }).catch(showError));
$("#detail-preview").addEventListener("click", () => { const file = focusedFile(); if (file) openPreview(file.asset_id); }); $("#open-large-preview").addEventListener("click", () => { const file = focusedFile(); if (file) openPreview(file.asset_id); }); $("#close-preview").addEventListener("click", closePreview); $("#preview-prev").addEventListener("click", () => previewStep(-1)); $("#preview-next").addEventListener("click", () => previewStep(1)); $("#preview-image").addEventListener("click", (event) => event.target.classList.toggle("zoomed"));
$("#close-detail").addEventListener("click", () => { state.selected.clear(); state.focusedId = null; $(".detail-pane").classList.remove("open"); renderLibrary(); updateSelection(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closePreview(); closeRuleModal(); closeMessage(); } if (!$("#preview-modal").hidden && event.key === "ArrowLeft") previewStep(-1); if (!$("#preview-modal").hidden && event.key === "ArrowRight") previewStep(1); });

(async function boot() {
  try { await Promise.all([refreshStatus(), loadRules(), loadJobs()]); }
  catch (error) { showError(error); }
  const poll = async () => {
    await Promise.all([refreshStatus().catch(() => {}), loadJobs().catch(() => {})]);
    const busy = state.jobs.some((job) => ["uploading", "queued", "running"].includes(job.status));
    setTimeout(poll, busy ? 1000 : 5000);
  };
  setTimeout(poll, 1000);
})();
