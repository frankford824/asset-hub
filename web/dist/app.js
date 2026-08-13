const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const PAGE_SIZE = 24;

const state = {
  activeJobId: null,
  lastDownloadJobId: null,
  jobs: [],
  previewZoom: false,
  previewIndex: -1,
  pageItems: [],
  page: 1,
  total: 0,
  query: "",
  searchSeq: 0,
  overlayMode: null, // busy | done | null
  debounceTimer: null,
};

const STATUS_LABEL = {
  queued: "排队中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
};

function fmtBytes(n) {
  const x = Number(n) || 0;
  if (x < 1024) return `${x} B`;
  if (x < 1024 ** 2) return `${(x / 1024).toFixed(1)} KB`;
  if (x < 1024 ** 3) return `${(x / 1024 ** 2).toFixed(1)} MB`;
  return `${(x / 1024 ** 3).toFixed(2)} GB`;
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function humanLabel(raw, status) {
  const text = String(raw || "").trim();
  if (!text) return STATUS_LABEL[status] || "处理中";
  if (/finalized|local_only|sync|OSS|provider|mock|jobs/i.test(text)) {
    return STATUS_LABEL[status] || "处理中";
  }
  return STATUS_LABEL[text] || text;
}

function assetPreviewUrl(id) {
  return `/api/v1/asset/preview?id=${encodeURIComponent(id)}`;
}

function assetDownloadUrl(id) {
  return `/api/v1/asset/download?id=${encodeURIComponent(id)}`;
}

function pageCount() {
  return Math.max(1, Math.ceil((state.total || 0) / PAGE_SIZE));
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || JSON.stringify(j);
    } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

function switchTab(name) {
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("#view-pack").hidden = name !== "pack";
  $("#view-library").hidden = name !== "library";
  if (name === "library" && !state.pageItems.length) {
    loadLibraryPage(1);
  }
}

function openDrawer() {
  $("#drawer-backdrop").hidden = false;
  $("#jobs-drawer").hidden = false;
}

function closeDrawer() {
  $("#drawer-backdrop").hidden = true;
  $("#jobs-drawer").hidden = true;
}

function openJobModal(job) {
  if (!job) return;
  $("#job-modal-sub").textContent = job.filename || job.id;
  $("#job-modal-body").innerHTML = `
    <dl class="kv">
      <div><dt>状态</dt><dd>${escapeHtml(STATUS_LABEL[job.status] || job.status)}</dd></div>
      <div><dt>进度</dt><dd>${(job.progress && job.progress.percent) || 0}%</dd></div>
      <div><dt>说明</dt><dd>${escapeHtml(humanLabel(job.progress && job.progress.label, job.status))}</dd></div>
      <div><dt>开始</dt><dd>${fmtTime(job.started_at)}</dd></div>
      <div><dt>完成</dt><dd>${fmtTime(job.finished_at)}</dd></div>
      <div><dt>编号</dt><dd style="font-family:var(--mono);font-size:12px">${escapeHtml(job.id)}</dd></div>
    </dl>
    ${job.error ? `<p class="form-msg err">${escapeHtml(job.error)}</p>` : ""}
    ${
      job.has_download
        ? `<div class="row-actions" style="margin-top:12px"><a class="btn btn-success" href="/api/v1/jobs/${job.id}/download">下载结果</a></div>`
        : ""
    }
  `;
  $("#job-modal").hidden = false;
}

function closeJobModal() {
  $("#job-modal").hidden = true;
}

/* —— pack overlay —— */
function showPackBusy(title, sub) {
  state.overlayMode = "busy";
  $("#pack-overlay").hidden = false;
  $("#pack-anim-busy").hidden = false;
  $("#pack-anim-done").hidden = true;
  $("#pack-overlay-title").textContent = title || "正在打包";
  $("#pack-overlay-sub").textContent = sub || "匹配资源并生成压缩包…";
  $("#pack-overlay-actions").hidden = true;
  $("#overlay-fill").style.width = "8%";
}

function updatePackOverlay(job) {
  if (state.overlayMode !== "busy" && state.overlayMode !== "done") return;
  if (!job) return;
  const pct = Math.max(8, (job.progress && job.progress.percent) || 0);
  $("#overlay-fill").style.width = `${Math.min(100, pct)}%`;
  $("#pack-overlay-sub").textContent = humanLabel(job.progress && job.progress.label, job.status);
  if (job.status === "done") {
    showPackDone(job);
  } else if (job.status === "failed") {
    state.overlayMode = "done";
    $("#pack-anim-busy").hidden = true;
    $("#pack-anim-done").hidden = true;
    $("#pack-overlay-title").textContent = "打包失败";
    $("#pack-overlay-sub").textContent = job.error || "请检查订单表后重试";
    $("#pack-overlay-actions").hidden = false;
    $("#overlay-download").hidden = true;
    $("#overlay-fill").style.width = "100%";
  }
}

function showPackDone(job) {
  state.overlayMode = "done";
  $("#pack-anim-busy").hidden = true;
  $("#pack-anim-done").hidden = false;
  // restart check animation
  const svg = $(".check-svg", $("#pack-anim-done"));
  if (svg) {
    const clone = svg.cloneNode(true);
    svg.replaceWith(clone);
  }
  $("#pack-overlay-title").textContent = "打包完成";
  $("#pack-overlay-sub").textContent = humanLabel(
    job.progress && job.progress.label,
    job.status
  );
  $("#overlay-fill").style.width = "100%";
  $("#pack-overlay-actions").hidden = false;
  if (job.has_download) {
    $("#overlay-download").hidden = false;
    $("#overlay-download").href = `/api/v1/jobs/${job.id}/download`;
    if ($("#auto-download").checked && state.lastDownloadJobId !== job.id) {
      state.lastDownloadJobId = job.id;
      const a = document.createElement("a");
      a.href = $("#overlay-download").href;
      a.click();
    }
  } else {
    $("#overlay-download").hidden = true;
  }
}

function closePackOverlay() {
  state.overlayMode = null;
  $("#pack-overlay").hidden = true;
}

/* —— preview lightbox with page nav —— */
function openPreviewAt(index) {
  const items = state.pageItems.filter((x) => x.previewable || isImageName(x.file_name));
  if (!items.length) return;
  state.previewIndex = Math.max(0, Math.min(index, items.length - 1));
  const asset = items[state.previewIndex];
  state.previewZoom = false;
  const img = $("#preview-img");
  img.hidden = true;
  img.classList.remove("zoomed");
  $("#preview-loading").hidden = false;
  $("#preview-title").textContent = asset.file_name || "预览";
  $("#preview-sub").textContent = `${state.previewIndex + 1}/${items.length} · ${asset.sku_code || "—"} · ${fmtBytes(asset.file_size)}`;
  $("#preview-download").href = assetDownloadUrl(asset.asset_id);
  $("#preview-modal").hidden = false;
  img.onload = () => {
    $("#preview-loading").hidden = true;
    img.hidden = false;
  };
  img.onerror = () => {
    $("#preview-loading").hidden = true;
    img.hidden = true;
    $("#preview-sub").textContent = "预览加载失败，可直接下载";
  };
  img.src = assetPreviewUrl(asset.asset_id);
}

function previewStep(delta) {
  const items = state.pageItems.filter((x) => x.previewable || isImageName(x.file_name));
  if (!items.length) return;
  const next = (state.previewIndex + delta + items.length) % items.length;
  openPreviewAt(next);
}

function closePreview() {
  $("#preview-modal").hidden = true;
  $("#preview-img").removeAttribute("src");
}

function isImageName(name) {
  return /\.(jpe?g|png|gif|webp|bmp|tiff?)$/i.test(name || "");
}

/* —— progress / jobs —— */
function renderProgress(job) {
  const steps = $$(".steps span");
  steps.forEach((el) => el.classList.remove("on", "done"));
  if (!job) {
    $("#progress-label").textContent = "暂无进行中的任务";
    $("#progress-pct").textContent = "—";
    $("#progress-fill").style.width = "0%";
    $("#meta-filename").textContent = "—";
    $("#meta-started").textContent = "—";
    $("#meta-finished").textContent = "—";
    $("#progress-actions").hidden = true;
    return;
  }
  const pct = (job.progress && job.progress.percent) || 0;
  $("#progress-pct").textContent = `${pct}%`;
  $("#progress-fill").style.width = `${Math.max(0, Math.min(100, pct))}%`;
  $("#progress-label").textContent = humanLabel(job.progress && job.progress.label, job.status);
  $("#meta-filename").textContent = job.filename || "—";
  $("#meta-started").textContent = fmtTime(job.started_at);
  $("#meta-finished").textContent = fmtTime(job.finished_at);
  if (job.status === "queued") steps[0].classList.add("on");
  else if (job.status === "running") {
    steps[0].classList.add("done");
    steps[1].classList.add("on");
  } else if (job.status === "done") steps.forEach((s) => s.classList.add("done"));
  else if (job.status === "failed") steps[1].classList.add("on");

  const canDl = job.has_download && job.status === "done";
  $("#progress-actions").hidden = !(canDl || job.status === "failed" || job.status === "done");
  $("#progress-download").hidden = !canDl;
  if (canDl) $("#progress-download").href = `/api/v1/jobs/${job.id}/download`;

  if (state.overlayMode === "busy" || state.overlayMode === "done") {
    updatePackOverlay(job);
  }
}

function renderJobs(jobs) {
  state.jobs = jobs;
  const preview = $("#jobs-preview");
  const list = $("#jobs-list");
  if (!jobs.length) {
    preview.innerHTML = `<div class="empty">暂无记录</div>`;
    list.innerHTML = `<div class="empty">暂无记录</div>`;
    if (!state.activeJobId) renderProgress(null);
    return;
  }
  if (!state.activeJobId) state.activeJobId = jobs[0].id;
  const active = jobs.find((j) => j.id === state.activeJobId) || jobs[0];
  renderProgress(active);

  const rowHtml = (j) => {
    const st = STATUS_LABEL[j.status] || j.status;
    const pct = (j.progress && j.progress.percent) || 0;
    return `<div class="job-row" data-job="${j.id}">
      <div>
        <strong>${escapeHtml(j.filename || j.id.slice(0, 8))}</strong>
        <span>${escapeHtml(st)} · ${pct}%</span>
      </div>
      ${
        j.has_download
          ? `<a class="btn btn-success btn-sm" href="/api/v1/jobs/${j.id}/download" onclick="event.stopPropagation()">下载</a>`
          : `<button class="btn btn-ghost btn-sm" type="button">详情</button>`
      }
    </div>`;
  };
  preview.innerHTML = jobs.slice(0, 4).map(rowHtml).join("");
  list.innerHTML = jobs.map(rowHtml).join("");
  [...$$(".job-row", preview), ...$$(".job-row", list)].forEach((el) => {
    el.addEventListener("click", () => {
      state.activeJobId = el.dataset.job;
      const job = state.jobs.find((j) => j.id === state.activeJobId);
      renderProgress(job);
      openJobModal(job);
    });
  });
}

async function refreshStatus() {
  const s = await api("/api/v1/status");
  const total = s.asset_count || 0;
  $("#meta-count").textContent = `素材 ${total.toLocaleString()}`;
  const ready = s.ready_for_pack || total > 0;
  const chip = $("#meta-ready");
  chip.textContent = ready ? (s.sync_complete ? "已就绪" : "同步中，可用") : "准备中";
  chip.className = `meta-chip ${ready ? "ok" : "warn"}`;
}

async function refreshJobs() {
  const data = await api("/api/v1/jobs?limit=30");
  renderJobs(data.jobs || []);
  const active = (data.jobs || []).find((j) => j.id === state.activeJobId);
  if (active && (active.status === "running" || active.status === "queued")) {
    try {
      const detail = await api(`/api/v1/jobs/${active.id}`);
      renderProgress(detail);
    } catch (_) {}
  }
}

/* —— library waterfall + live search + pager —— */
function renderPager() {
  const pages = pageCount();
  const show = state.total > 0;
  $("#pager").hidden = !show;
  $("#page-info").textContent = `${state.page} / ${pages}`;
  $("#page-prev").disabled = state.page <= 1;
  $("#page-next").disabled = state.page >= pages;
  $("#lib-total").textContent = state.total
    ? `共 ${state.total.toLocaleString()} 项`
    : "无结果";
}

function renderWaterfall(rows) {
  const box = $("#results");
  state.pageItems = rows;
  if (!rows.length) {
    box.innerHTML = `<div class="empty wide">没有找到匹配素材</div>`;
    return;
  }
  box.innerHTML = rows
    .map((r, idx) => {
      const previewable = r.previewable || isImageName(r.file_name);
      const thumb = previewable
        ? `<img src="${assetPreviewUrl(r.asset_id)}" alt="" loading="lazy" />`
        : `<div class="ph">${escapeHtml((r.file_name || "").split(".").pop() || "FILE").toUpperCase()}</div>`;
      return `<article class="asset-card" data-idx="${idx}" data-id="${encodeURIComponent(r.asset_id)}" data-previewable="${previewable ? "1" : "0"}">
        <div class="thumb">${thumb}</div>
        <div class="asset-info">
          <strong title="${escapeHtml(r.file_name || "")}">${escapeHtml(r.file_name || r.asset_id)}</strong>
          <em>${escapeHtml(r.sku_code || "—")} · ${fmtBytes(r.file_size)}</em>
        </div>
      </article>`;
    })
    .join("");

  $$(".asset-card", box).forEach((card) => {
    card.addEventListener("click", () => {
      const idx = Number(card.dataset.idx);
      const asset = state.pageItems[idx];
      if (!asset) return;
      if (card.dataset.previewable === "1") {
        const previewables = state.pageItems.filter(
          (x) => x.previewable || isImageName(x.file_name)
        );
        const pidx = previewables.findIndex((x) => x.asset_id === asset.asset_id);
        openPreviewAt(pidx >= 0 ? pidx : 0);
      } else {
        window.location.href = assetDownloadUrl(asset.asset_id);
      }
    });
  });
}

async function loadLibraryPage(page, { quiet } = {}) {
  const seq = ++state.searchSeq;
  state.page = Math.max(1, page);
  const offset = (state.page - 1) * PAGE_SIZE;
  if (!quiet) {
    $("#lib-loading").hidden = false;
    $("#search-spin").hidden = false;
  } else {
    $("#search-spin").hidden = false;
  }
  try {
    const params = new URLSearchParams({
      q: state.query,
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    const data = await api(`/api/v1/search?${params}`);
    if (seq !== state.searchSeq) return;
    state.total = data.total || 0;
    const maxPage = pageCount();
    if (state.page > maxPage) {
      return loadLibraryPage(maxPage, { quiet });
    }
    renderWaterfall(data.results || []);
    renderPager();
  } catch (e) {
    if (seq !== state.searchSeq) return;
    $("#results").innerHTML = `<div class="empty wide">${escapeHtml(e.message || String(e))}</div>`;
    $("#pager").hidden = true;
  } finally {
    if (seq === state.searchSeq) {
      $("#lib-loading").hidden = true;
      $("#search-spin").hidden = true;
    }
  }
}

function scheduleLiveSearch() {
  clearTimeout(state.debounceTimer);
  state.debounceTimer = setTimeout(() => {
    state.query = $("#q").value.trim();
    loadLibraryPage(1, { quiet: true });
  }, 280);
}

async function submitPack(ev) {
  ev.preventDefault();
  const msg = $("#pack-msg");
  const file = $("#excel").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("super_dir_name", $("#super").value.trim());
  msg.hidden = true;
  $("#pack-submit").disabled = true;
  showPackBusy("正在提交", "上传订单表…");
  try {
    const data = await api("/api/v1/jobs", { method: "POST", body: fd });
    state.activeJobId = data.job_id;
    state.lastDownloadJobId = null;
    showPackBusy("正在打包", "匹配资源并生成压缩包…");
    $("#excel").value = "";
    await refreshJobs();
  } catch (e) {
    closePackOverlay();
    msg.hidden = false;
    msg.className = "form-msg err";
    msg.textContent = String(e.message || e);
  } finally {
    $("#pack-submit").disabled = false;
  }
}

// events
$$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
$("#pack-form").addEventListener("submit", submitPack);
$("#q").addEventListener("input", scheduleLiveSearch);
$("#page-prev").addEventListener("click", () => loadLibraryPage(state.page - 1));
$("#page-next").addEventListener("click", () => loadLibraryPage(state.page + 1));
$("#btn-refresh-meta").addEventListener("click", () => {
  refreshStatus().catch(console.error);
  refreshJobs().catch(console.error);
  if (!$("#view-library").hidden) loadLibraryPage(state.page);
});
$("#btn-open-jobs").addEventListener("click", openDrawer);
$("#btn-job-detail").addEventListener("click", () => {
  openJobModal(state.jobs.find((j) => j.id === state.activeJobId));
});
$("#drawer-backdrop").addEventListener("click", closeDrawer);
$$('[data-close="drawer"]').forEach((b) => b.addEventListener("click", closeDrawer));
$$('[data-close="job-modal"]').forEach((b) => b.addEventListener("click", closeJobModal));
$$('[data-close="preview-modal"]').forEach((b) => b.addEventListener("click", closePreview));
$("#job-modal").addEventListener("click", (e) => {
  if (e.target === $("#job-modal")) closeJobModal();
});
$("#preview-modal").addEventListener("click", (e) => {
  if (e.target === $("#preview-modal")) closePreview();
});
$("#preview-img").addEventListener("click", () => {
  state.previewZoom = !state.previewZoom;
  $("#preview-img").classList.toggle("zoomed", state.previewZoom);
});
$("#preview-prev").addEventListener("click", () => previewStep(-1));
$("#preview-next").addEventListener("click", () => previewStep(1));
$("#overlay-close").addEventListener("click", closePackOverlay);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closePreview();
    closeJobModal();
    closeDrawer();
    if (state.overlayMode === "done") closePackOverlay();
  }
  if (!$("#preview-modal").hidden) {
    if (e.key === "ArrowLeft") previewStep(-1);
    if (e.key === "ArrowRight") previewStep(1);
  }
});

(async function boot() {
  try {
    await refreshStatus();
    await refreshJobs();
  } catch (e) {
    $("#meta-ready").textContent = "服务异常";
    $("#meta-ready").className = "meta-chip warn";
    console.error(e);
  }
  setInterval(() => {
    refreshStatus().catch(() => {});
    refreshJobs().catch(() => {});
  }, 2500);
})();
