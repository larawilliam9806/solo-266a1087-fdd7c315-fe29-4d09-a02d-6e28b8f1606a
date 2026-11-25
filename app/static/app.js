const MAX_WARNINGS = 20;
const MARK_LABEL = { normal: "正常", warning: "预警", retracted: "已撤稿" };

let state = null;
let pollTimer = null;

const $ = (id) => document.getElementById(id);

function toast(msg, kind = "error") {
  const el = $("toast");
  el.textContent = msg;
  el.className = kind;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 3500);
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  let body = null;
  try { body = await res.json(); } catch { /* no json */ }
  if (!res.ok) throw new Error((body && body.error) || `请求失败 (${res.status})`);
  return body;
}

// ------------------------------------------------------------------ state --

async function refresh() {
  state = await api("/api/state");
  renderDataset();
  renderPapers();
  renderLive();
  renderSimulation();
  if (state.running && !pollTimer) {
    pollTimer = setInterval(refresh, 1500);
  } else if (!state.running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  $("run-btn").disabled = state.running || !state.dataset;
  $("sim-status").textContent = state.running ? "推演进行中…" : "";
}

function renderDataset() {
  const el = $("dataset-info");
  if (!state.dataset) {
    el.innerHTML = '<span class="tag warning">尚未上传数据</span>';
    return;
  }
  el.innerHTML =
    `当前数据：<b>${state.conclusion_count}</b> 条结论、` +
    `<b>${state.paper_count}</b> 篇文献、<b>${state.dataset.row_count}</b> 条 CSV 记录` +
    `（上传于 ${state.dataset.uploaded_at.replace("T", " ")}）`;
}

function renderPapers() {
  const body = $("papers-body");
  const wc = $("warning-counter");
  wc.textContent = `预警 ${state.counts.warning} / ${MAX_WARNINGS}`;
  wc.classList.toggle("full", state.warning_limit_reached);

  const q = $("paper-filter").value.trim().toLowerCase();
  const mf = $("mark-filter").value;
  const papers = (state.papers || []).filter((p) => {
    if (mf !== "all" && p.mark !== mf) return false;
    if (q && !p.doi.includes(q)) return false;
    return true;
  });

  if (!state.dataset) {
    body.innerHTML = '<tr><td colspan="3" class="empty">上传 CSV 后显示文献列表</td></tr>';
    return;
  }
  if (!papers.length) {
    body.innerHTML = '<tr><td colspan="3" class="empty">无匹配文献</td></tr>';
    return;
  }
  body.innerHTML = papers.map((p) => `
    <tr>
      <td><code>${escapeHtml(p.doi)}</code></td>
      <td><span class="tag ${p.mark}">${MARK_LABEL[p.mark]}</span></td>
      <td class="actions">
        ${["warning", "retracted", "normal"].map((m) => `
          <button class="mini choose-${m} ${p.mark === m ? "active" : ""}"
                  data-doi="${escapeHtml(p.doi)}" data-mark="${m}">
            ${MARK_LABEL[m]}
          </button>`).join("")}
      </td>
    </tr>`).join("");

  body.querySelectorAll("button.mini").forEach((btn) => {
    btn.addEventListener("click", () => markPaper(btn.dataset.doi, btn.dataset.mark));
  });
}

async function markPaper(doi, mark) {
  try {
    await api("/api/papers/mark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doi, mark }),
    });
    await refresh();
  } catch (err) {
    toast(err.message);
    await refresh();
  }
}

function renderLive() {
  const card = $("live-card");
  if (!state.dataset) { card.hidden = true; return; }
  card.hidden = false;
  const ids = state.live_unsupported;
  $("live-body").innerHTML = ids.length
    ? `<p>共 <span class="big-num">${ids.length}</span> 条结论已无完整引文组：</p>
       <ul class="cid-list">${ids.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`
    : '<p class="hint">当前标记下暂无失据结论。</p>';
}

// ------------------------------------------------------------- simulation --

function renderSimulation() {
  const sim = state.latest_simulation;
  const card = $("result-card");
  if (!sim) { card.hidden = true; return; }
  card.hidden = false;

  $("stale-banner").hidden = !sim.stale;
  const failedBanner = $("failed-banner");
  if (sim.status === "failed") {
    failedBanner.hidden = false;
    $("failed-error").textContent = sim.error || "";
  } else {
    failedBanner.hidden = true;
  }

  if (sim.status === "running") {
    $("result-body").innerHTML =
      `<p class="hint">推演 #${sim.id} 正在运行（启动于 ${sim.started_at.replace("T", " ")}）…</p>`;
    return;
  }
  if (sim.status === "failed" && !sim.result) {
    $("result-body").innerHTML = '<p class="hint">没有可展示的成功结果。</p>';
    return;
  }
  if (sim.status === "failed" && sim.result) {
    $("result-body").innerHTML = resultHtml(sim) +
      '<p class="result-meta">注：上方为上一次成功推演的保留结果。</p>';
    return;
  }
  $("result-body").innerHTML = resultHtml(sim);
}

function resultHtml(sim) {
  const r = sim.result;
  const already = r.already_unsupported || [];
  const affected = r.affected_conclusions || [];
  const selected = r.selected_dois || [];
  return `
    <div class="result-block">
      <h3>① 已失据结论（启动快照下，共 ${already.length} 条）</h3>
      ${already.length
        ? `<ul class="cid-list">${already.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`
        : '<p class="hint">无。</p>'}
    </div>
    <div class="result-block">
      <h3>② 最优预警 DOI 组合（上限 k=${r.k_requested}，实际选用 ${r.k_used} 篇）</h3>
      ${selected.length
        ? `<div class="chips">${selected.map((d) =>
            `<span class="chip warning">${escapeHtml(d)}</span>`).join("")}</div>`
        : '<p class="hint">在给定上限内没有可使结论新增失据的预警组合。</p>'}
      <p class="result-meta">
        推演枚举组合数：${r.evaluated_combinations}；快照时间：${(sim.snapshot_captured_at || "").replace("T", " ")}
      </p>
    </div>
    <div class="result-block">
      <h3>③ 该组合将新增失据的结论（共 <span class="big-num">${r.affected_count}</span> 条）</h3>
      ${affected.length
        ? `<ul class="cid-list">${affected.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`
        : '<p class="hint">无新增失据结论。</p>'}
    </div>
    <p class="result-meta">
      可推演（killable）结论共 ${r.killable_conclusions.length} 条；
      快照中预警文献 ${r.warning_total} 篇。
      ${sim.stale ? "本结果基于旧标记，已过期。" : ""}
    </p>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ----------------------------------------------------------------- events --

$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("csv-file").files[0];
  if (!file) return toast("请选择 CSV 文件");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("/api/dataset", { method: "POST", body: fd });
    toast(`上传成功：${r.row_count} 条记录`, "ok");
    await refresh();
  } catch (err) {
    toast(err.message);
  }
});

$("paper-filter").addEventListener("input", renderPapers);
$("mark-filter").addEventListener("change", renderPapers);

$("sim-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const k = Number($("k-input").value);
  if (!Number.isInteger(k) || k < 0) return toast("k 必须是非负整数");
  if (k > MAX_WARNINGS) return toast(`k 不能超过 ${MAX_WARNINGS}`);
  try {
    await api("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ k }),
    });
    toast("推演已启动", "ok");
    await refresh();
  } catch (err) {
    toast(err.message);
  }
});

refresh().catch((err) => toast(err.message));
