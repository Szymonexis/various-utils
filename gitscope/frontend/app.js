/* gitscope frontend.
 * The backend ships raw per-commit data; everything you see is aggregated here so that
 * merging or hiding people never needs another trip to git. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const fmt = (n) => (n ?? 0).toLocaleString("en-US");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const PALETTE = ["#f27b54", "#5eb8f0", "#a78bfa", "#f5c451", "#5fcf80", "#f472b6", "#38d9c9", "#fb923c", "#93c5fd", "#c4b5fd", "#fde68a", "#86efac", "#f9a8d4", "#67e8f9", "#fca5a5", "#d9f99d"];
const OTHERS = "#5b6674";
const CSS = getComputedStyle(document.documentElement);
const color = (name) => CSS.getPropertyValue(name).trim();

const state = {
  config: null,
  repoPath: "",
  data: null,          // /api/analyze payload
  ownership: null,     // /api/ownership result
  identities: { merges: {}, excluded: [], names: {} },
  groups: [],          // computed people after merges/excludes
  groupByKey: new Map(),
  opts: { coauthors: false, merges: true, all: false, ignore: "", metric: "commits", gran: "month", ftm: "churn", sort: "net" },
  commitsShown: 100,
  pollTimer: null,
};
const charts = {};
const chartCfgs = {}; // last config per canvas, so the export can redraw at high resolution

Chart.defaults.color = color("--muted");
Chart.defaults.borderColor = color("--line");
Chart.defaults.font.family = color("--sans") || "system-ui, sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.plugins.legend.labels.boxWidth = 10;
Chart.defaults.plugins.legend.labels.boxHeight = 10;
Chart.defaults.plugins.tooltip.backgroundColor = "#111820";
Chart.defaults.plugins.tooltip.borderColor = color("--line");
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.animation = false;

/* ------------------------------------------------------------------ api */

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error(body?.detail || `${res.status} ${res.statusText}`);
  return body;
}
const q = (params) => new URLSearchParams(params).toString();

/* ------------------------------------------------------------------ ui plumbing */

function notice(msg, kind = "error") {
  const el = $("#notice");
  if (!msg) { el.hidden = true; return; }
  el.textContent = msg;
  el.className = "notice" + (kind === "info" ? " info" : "");
  el.hidden = false;
}
function loading(text) {
  const el = $("#loading");
  if (!text) { el.hidden = true; return; }
  $("#loading-text").textContent = text;
  el.hidden = false;
}
function storageKey(name) { return `gitscope:${state.repoPath}:${name}`; }
function saveLocalOpts() {
  if (!state.repoPath) return;
  const { coauthors, merges, all, ignore, gran, metric, ftm, sort } = state.opts;
  localStorage.setItem(storageKey("opts"), JSON.stringify({ coauthors, merges, all, ignore, gran, metric, ftm, sort }));
}
function loadLocalOpts() {
  try { Object.assign(state.opts, JSON.parse(localStorage.getItem(storageKey("opts")) || "{}")); } catch { /* ignore */ }
  $("#opt-coauthors").checked = state.opts.coauthors;
  $("#opt-merges").checked = state.opts.merges;
  $("#opt-all").checked = state.opts.all;
  $("#opt-ignore").value = state.opts.ignore;
  $("#roster-sort").value = state.opts.sort;
  $$("[data-metric]").forEach((b) => b.classList.toggle("on", b.dataset.metric === state.opts.metric));
  $$("[data-gran]").forEach((b) => b.classList.toggle("on", b.dataset.gran === state.opts.gran));
  $$("[data-ftm]").forEach((b) => b.classList.toggle("on", b.dataset.ftm === state.opts.ftm));
}

/* ------------------------------------------------------------------ loading a repo */

async function openRepo(path, { reread = false } = {}) {
  path = (path || "").trim();
  if (!path) return;
  notice(null);
  loading("Checking repository…");
  try {
    const info = await api(`/api/repo?${q({ path })}`);
    state.repoPath = info.path;
    $("#repo-path").value = info.path;
    history.replaceState(null, "", `?repo=${encodeURIComponent(info.path)}`);
    if (!reread) loadLocalOpts();
    else saveLocalOpts();
    loading(`Reading git history of ${info.name}…`);
    const [data, identities] = await Promise.all([
      api(`/api/analyze?${q({ path: info.path, all: state.opts.all, ignore: state.opts.ignore })}`),
      api(`/api/identities?${q({ path: info.path })}`),
    ]);
    state.data = data;
    state.identities = { merges: {}, excluded: [], names: {}, ...identities };
    state.ownership = null;
    stopPolling();
    const own = await api(`/api/ownership?${q({ path: info.path, ignore: state.opts.ignore })}`);
    if (own.status === "done") state.ownership = own.result;
    renderOwnershipState(own);
    $("#welcome").hidden = true;
    $("#layout").hidden = false;
    $("#btn-export").hidden = false;
    renderRepoMeta();
    renderAll();
    if (own.status === "running") startPolling();
  } catch (err) {
    notice(err.message);
  } finally {
    loading(null);
  }
}

function renderRepoMeta() {
  const r = state.data.repo;
  const meta = $("#repo-meta");
  $("#meta-branch").textContent = r.branch;
  $("#meta-head").textContent = r.head.slice(0, 8);
  $("#meta-commits").textContent = `${fmt(state.data.commits.length)} commits${state.opts.all ? " (all branches)" : ""}`;
  meta.hidden = false;
  document.title = `${r.name} · gitscope`;
}

/* ------------------------------------------------------------------ identities */

function canonical(key) {
  const seen = new Set();
  let k = key;
  while (state.identities.merges[k] && !seen.has(k)) { seen.add(k); k = state.identities.merges[k]; }
  return k;
}
function persistIdentities() {
  clearTimeout(persistIdentities.t);
  persistIdentities.t = setTimeout(async () => {
    try {
      await api(`/api/identities?${q({ path: state.repoPath })}`, {
        method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(state.identities),
      });
    } catch (err) { notice(`Could not save identity changes: ${err.message}`); }
  }, 400);
}
function mergeInto(aliasKey, targetKey) {
  if (!targetKey || aliasKey === targetKey) return;
  const target = canonical(targetKey);
  if (target === aliasKey) return; // would create a cycle
  // everything that currently points at alias now points at the target
  for (const [k, v] of Object.entries(state.identities.merges)) if (v === aliasKey) state.identities.merges[k] = target;
  state.identities.merges[aliasKey] = target;
  state.identities.excluded = state.identities.excluded.filter((k) => k !== aliasKey);
  persistIdentities(); renderAll();
}
function unmerge(aliasKey) {
  delete state.identities.merges[aliasKey];
  persistIdentities(); renderAll();
}
function toggleHidden(key) {
  const ex = new Set(state.identities.excluded);
  ex.has(key) ? ex.delete(key) : ex.add(key);
  state.identities.excluded = [...ex];
  persistIdentities(); renderAll();
}
function mergeIdenticalNames() {
  const byName = new Map();
  for (const g of visibleGroups()) {
    const n = g.name.trim().toLowerCase();
    if (!n) continue;
    (byName.get(n) || byName.set(n, []).get(n)).push(g);
  }
  let merged = 0;
  for (const list of byName.values()) {
    if (list.length < 2) continue;
    list.sort((a, b) => b.commits - a.commits);
    for (const g of list.slice(1)) { state.identities.merges[g.key] = list[0].key; merged++; }
  }
  if (!merged) { notice("No two people share a name right now.", "info"); return; }
  persistIdentities(); renderAll();
  notice(`Merged ${merged} identit${merged === 1 ? "y" : "ies"} by name. Use ✕ on a person to split any of them off again.`, "info");
}
function resetIdentities() {
  if (!confirm("Undo every merge, hide and rename for this repository?")) return;
  state.identities = { merges: {}, excluded: [], names: {} };
  persistIdentities(); renderAll();
}
function rename(key, name) {
  name = name.trim();
  if (name) state.identities.names[key] = name; else delete state.identities.names[key];
  persistIdentities(); renderAll();
}

/* ------------------------------------------------------------------ aggregation */

function localDate(c) { return new Date((c.t + c.o * 60) * 1000); } // read with UTC getters = author local time
function dayKey(d) { return d.toISOString().slice(0, 10); }
function bucketKey(d, gran) {
  if (gran === "year") return String(d.getUTCFullYear());
  if (gran === "month") return d.toISOString().slice(0, 7);
  const monday = new Date(d); monday.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7)); monday.setUTCHours(0, 0, 0, 0);
  return dayKey(monday);
}
function nextBucket(key, gran) {
  if (gran === "year") return String(Number(key) + 1);
  if (gran === "month") { const [y, m] = key.split("-").map(Number); return new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 7); }
  const d = new Date(key + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + 7); return dayKey(d);
}
function bucketRange(commits, gran) {
  if (!commits.length) return [];
  const keys = [];
  let k = bucketKey(localDate(commits[0]), gran);
  const last = bucketKey(localDate(commits[commits.length - 1]), gran);
  keys.push(k);
  let guard = 0;
  while (k !== last && guard++ < 5000) { k = nextBucket(k, gran); keys.push(k); }
  return keys;
}
function commitPeople(c) {
  const a = state.data.authors;
  const keys = new Set([canonical(a[c.a].key)]);
  if (state.opts.coauthors) for (const ci of c.c) keys.add(canonical(a[ci].key));
  return keys;
}
function includedCommits() {
  return state.data.commits.filter((c) => state.opts.merges || !c.m);
}

function buildGroups() {
  const { authors, ext_by_author } = state.data;
  const excluded = new Set(state.identities.excluded);
  const groups = new Map();
  const ensure = (key, fallbackName) => {
    let g = groups.get(key);
    if (!g) {
      g = { key, name: fallbackName, email: "", members: [], commits: 0, add: 0, del: 0, owned: 0, ext: {}, ownedExt: {}, first: Infinity, last: 0, days: new Set(), hidden: excluded.has(key), color: OTHERS };
      groups.set(key, g);
    }
    return g;
  };
  for (const a of authors) {
    const g = ensure(canonical(a.key), a.name);
    if (a.key === g.key) { g.email = a.email; g.name = a.name; }
    g.members.push(a);
  }
  for (const c of includedCommits()) {
    for (const key of commitPeople(c)) {
      const g = ensure(key, key);
      g.commits++; g.add += c.add; g.del += c.del;
      if (c.t < g.first) g.first = c.t; if (c.t > g.last) g.last = c.t;
      g.days.add(dayKey(localDate(c)));
    }
  }
  for (const [idx, exts] of Object.entries(ext_by_author)) {
    const g = ensure(canonical(authors[idx].key), authors[idx].name);
    for (const [ext, [add, del]] of Object.entries(exts)) { const e = g.ext[ext] || (g.ext[ext] = [0, 0]); e[0] += add; e[1] += del; }
  }
  if (state.ownership) {
    for (const [key, exts] of Object.entries(state.ownership.by_author)) {
      const g = ensure(canonical(key), key);
      for (const [ext, n] of Object.entries(exts)) { g.owned += n; g.ownedExt[ext] = (g.ownedExt[ext] || 0) + n; }
    }
  }
  for (const g of groups.values()) {
    if (state.identities.names[g.key]) g.name = state.identities.names[g.key];
    g.net = g.add - g.del;
    g.activeDays = g.days.size;
  }
  // colours follow overall rank so they stay stable while you play with sort order
  const ranked = [...groups.values()].filter((g) => !g.hidden).sort((a, b) => (b.add + b.del) - (a.add + a.del) || b.commits - a.commits);
  ranked.forEach((g, i) => { g.color = i < PALETTE.length ? PALETTE[i] : OTHERS; });
  state.groups = [...groups.values()];
  state.groupByKey = groups;
}
function visibleGroups() { return state.groups.filter((g) => !g.hidden); }
function sortedGroups(list, by = state.opts.sort) {
  const s = [...list];
  if (by === "name") s.sort((a, b) => a.name.localeCompare(b.name));
  else if (by === "commits") s.sort((a, b) => b.commits - a.commits);
  else if (by === "owned") s.sort((a, b) => b.owned - a.owned);
  else s.sort((a, b) => (b.add + b.del) - (a.add + a.del));
  return s;
}
function topGroups(n, metricFn) {
  const vis = visibleGroups().filter((g) => metricFn(g) > 0).sort((a, b) => metricFn(b) - metricFn(a));
  return { top: vis.slice(0, n), rest: vis.slice(n) };
}

/* ------------------------------------------------------------------ rendering */

function renderAll() {
  buildGroups();
  renderRoster();
  renderSummary();
  renderLinesChart();
  renderOwnershipChart();
  renderActivity();
  renderCumulative();
  renderHeatmap();
  renderTypes();
  renderTopFiles();
  renderTopDays();
  renderCommitFilters();
  state.commitsShown = 100;
  renderCommits();
}

let rosterShowAll = false;
function renderRoster() {
  const list = $("#roster");
  const term = ($("#roster-search").value || "").trim().toLowerCase();
  let groups = sortedGroups(state.groups);
  if (term) groups = groups.filter((g) => g.name.toLowerCase().includes(term) || g.email.includes(term) || g.members.some((m) => m.name.toLowerCase().includes(term) || m.email.includes(term)));
  const CAP = 150;
  const capped = !rosterShowAll && groups.length > CAP;
  const shown = capped ? groups.slice(0, CAP) : groups;
  list.innerHTML = shown.map((g) => {
    const members = g.members.filter((m) => m.key !== g.key);
    const aliasNames = g.members.find((m) => m.key === g.key)?.aliases || [];
    return `<li class="person ${g.hidden ? "hidden-person" : ""}" style="border-left-color:${g.color}" data-key="${esc(g.key)}">
      <div class="person-top">
        <span class="person-name" title="Click the pencil to rename">${esc(g.name)}</span>
        <button class="icon-btn" data-act="rename" title="Rename">✎</button>
        <button class="icon-btn" data-act="hide" title="${g.hidden ? "Show again" : "Hide from all stats"}">${g.hidden ? "◌" : "◉"}</button>
      </div>
      <div class="person-email" title="${esc(g.email)}">${esc(g.email || g.key)}${aliasNames.length ? ` <span title="Other names on this email">+ ${esc(aliasNames.join(", "))}</span>` : ""}</div>
      <div class="person-nums">
        <span><b>${fmt(g.commits)}</b> commits</span>
        <span class="add"><b>+${fmt(g.add)}</b></span>
        <span class="del"><b>−${fmt(g.del)}</b></span>
        <span class="own" title="Lines still present at HEAD (git blame)">${state.ownership ? `<b>${fmt(g.owned)}</b> owned` : ""}</span>
      </div>
      <div class="person-actions">
        <select data-act="merge" ${g.hidden ? "disabled" : ""}><option value="">Merge into…</option></select>
      </div>
      ${members.length ? `<ul class="person-members">${members.map((m) => `<li><span title="${esc(m.email || m.key)}">${esc(m.name)} <em>${esc(m.email)}</em></span><button class="icon-btn" data-act="unmerge" data-alias="${esc(m.key)}" title="Split off again">✕</button></li>`).join("")}</ul>` : ""}
    </li>`;
  }).join("") + (capped ? `<li><button class="btn btn-block" data-act="show-all">Show all ${fmt(groups.length)} people</button></li>` : "") + (!groups.length ? `<li class="hint">Nobody matches.</li>` : "");
}
// The merge dropdown is filled on demand: with hundreds of contributors, one full list per row is too much DOM.
function fillMergeSelect(sel) {
  if (sel.dataset.filled) return;
  const self = sel.closest(".person").dataset.key;
  sel.insertAdjacentHTML("beforeend", sortedGroups(visibleGroups(), "name").filter((o) => o.key !== self).map((o) => `<option value="${esc(o.key)}">${esc(o.name)}</option>`).join(""));
  sel.dataset.filled = "1";
}
$("#roster").addEventListener("focusin", (e) => { const sel = e.target.closest("select[data-act=merge]"); if (sel) fillMergeSelect(sel); });
$("#roster").addEventListener("mousedown", (e) => { const sel = e.target.closest("select[data-act=merge]"); if (sel) fillMergeSelect(sel); });
$("#roster-search").addEventListener("input", () => renderRoster());
$("#btn-merge-names").addEventListener("click", mergeIdenticalNames);
$("#btn-reset-identities").addEventListener("click", resetIdentities);

$("#roster").addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-act=merge]");
  if (!sel) return;
  const key = sel.closest(".person").dataset.key;
  mergeInto(key, sel.value);
});
$("#roster").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn || btn.tagName === "SELECT") return;
  if (btn.dataset.act === "show-all") { rosterShowAll = true; renderRoster(); return; }
  const key = btn.closest(".person").dataset.key;
  if (btn.dataset.act === "hide") toggleHidden(key);
  else if (btn.dataset.act === "unmerge") unmerge(btn.dataset.alias);
  else if (btn.dataset.act === "rename") startRename(btn.closest(".person"), key);
});
function startRename(li, key) {
  const el = $(".person-name", li);
  if (el.isContentEditable) return;
  el.contentEditable = "true"; el.classList.add("editing"); el.focus();
  document.getSelection()?.selectAllChildren(el);
  const finish = (commit) => {
    el.removeEventListener("blur", onBlur); el.removeEventListener("keydown", onKey);
    el.contentEditable = "false"; el.classList.remove("editing");
    if (commit) rename(key, el.textContent); else renderRoster();
  };
  const onBlur = () => finish(true);
  const onKey = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); finish(true); } if (ev.key === "Escape") { ev.preventDefault(); finish(false); } };
  el.addEventListener("blur", onBlur); el.addEventListener("keydown", onKey);
}

function renderSummary() {
  const vis = visibleGroups();
  const commits = includedCommits().filter((c) => [...commitPeople(c)].some((k) => !state.groupByKey.get(k)?.hidden));
  const add = vis.reduce((s, g) => s + g.add, 0);
  const del = vis.reduce((s, g) => s + g.del, 0);
  const owned = vis.reduce((s, g) => s + g.owned, 0);
  const first = commits[0], last = commits[commits.length - 1];
  const span = first && last ? Math.max(1, Math.round((last.t - first.t) / 86400)) : 0;
  const days = new Set(commits.map((c) => dayKey(localDate(c)))).size;
  const dateStr = (c) => localDate(c).toISOString().slice(0, 10);
  $("#summary").innerHTML = [
    ["commits", fmt(commits.length)],
    ["people", fmt(vis.length)],
    ["lines added", `<span class="add">+${fmt(add)}</span>`],
    ["lines deleted", `<span class="del">−${fmt(del)}</span>`],
    ["lines at HEAD", state.ownership ? fmt(owned) : "<span style='color:var(--muted)'>not computed</span>"],
    ["active days", `${fmt(days)} <span style='font-size:13px;color:var(--muted)'>of ${fmt(span)}</span>`],
    ["history", first ? `${dateStr(first)} → ${dateStr(last)}` : "-", "stat-wide"],
  ].map(([label, val, cls]) => `<div class="stat ${cls || ""}"><b>${val}</b><span>${label}</span></div>`).join("");
  if (state.data.ignored_files) notice(`${fmt(state.data.ignored_files)} paths skipped by your ignore patterns.`, "info");
}

function chart(id, cfg) {
  chartCfgs[id] = cfg;
  if (charts[id]) { charts[id].destroy(); }
  charts[id] = new Chart($(id), cfg);
  return charts[id];
}

function renderLinesChart() {
  const groups = sortedGroups(visibleGroups(), "net").slice(0, 20);
  $("#wrap-lines").style.height = `${Math.max(220, 40 + groups.length * 26)}px`;
  const datasets = [
    { label: "added", data: groups.map((g) => g.add), backgroundColor: color("--add") },
    { label: "deleted", data: groups.map((g) => g.del), backgroundColor: color("--del") },
  ];
  if (state.ownership) datasets.push({ label: "owned today", data: groups.map((g) => g.owned), backgroundColor: color("--own") });
  chart("#chart-lines", {
    type: "bar",
    data: { labels: groups.map((g) => g.name), datasets },
    options: {
      indexAxis: "y", maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.raw)}` } } },
      scales: { x: { ticks: { callback: (v) => fmt(v), precision: 0 }, grid: { color: color("--line") } }, y: { grid: { display: false } } },
    },
  });
}

function renderOwnershipState(job) {
  const idle = $("#ownership-idle"), prog = $("#ownership-progress"), wrap = $("#wrap-own");
  idle.hidden = prog.hidden = wrap.hidden = true;
  if (job.status === "done") { wrap.hidden = false; return; }
  if (job.status === "running" || job.status === "queued") {
    prog.hidden = false;
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    $("#ownership-fill").style.width = `${pct}%`;
    $("#ownership-status").textContent = job.total ? `Blaming ${fmt(job.done)} of ${fmt(job.total)} files (${pct}%)` : "Listing files…";
    return;
  }
  idle.hidden = false;
  if (job.status === "error") $("#ownership-hint").textContent = `Last attempt failed: ${job.error}`;
}
function renderOwnershipChart() {
  if (!state.ownership) return;
  const { top, rest } = topGroups(10, (g) => g.owned);
  const labels = top.map((g) => g.name), data = top.map((g) => g.owned), colors = top.map((g) => g.color);
  const restSum = rest.reduce((s, g) => s + g.owned, 0);
  if (restSum) { labels.push(`${rest.length} others`); data.push(restSum); colors.push(OTHERS); }
  const total = data.reduce((s, n) => s + n, 0) || 1;
  chart("#chart-own", {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: color("--panel"), borderWidth: 2 }] },
    options: {
      maintainAspectRatio: false, cutout: "58%",
      plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: (c) => ` ${fmt(c.raw)} lines (${((c.raw / total) * 100).toFixed(1)}%)` } } },
    },
  });
}

async function startOwnership() {
  const btn = $("#btn-ownership"); btn.disabled = true;
  try {
    const job = await api(`/api/ownership?${q({ path: state.repoPath, ignore: state.opts.ignore })}`, { method: "POST" });
    renderOwnershipState(job);
    if (job.status === "done") { state.ownership = job.result; renderAll(); }
    else startPolling();
  } catch (err) { notice(err.message); }
  finally { btn.disabled = false; }
}
function stopPolling() { clearInterval(state.pollTimer); state.pollTimer = null; }
function startPolling() {
  stopPolling();
  const path = state.repoPath;
  state.pollTimer = setInterval(async () => {
    if (state.repoPath !== path) return stopPolling();
    try {
      const job = await api(`/api/ownership?${q({ path, ignore: state.opts.ignore })}`);
      renderOwnershipState(job);
      if (job.status === "done") { stopPolling(); state.ownership = job.result; renderAll(); }
      else if (job.status === "error") stopPolling();
    } catch (err) { stopPolling(); notice(err.message); }
  }, 1500);
}

function renderActivity() {
  const gran = state.opts.gran, metric = state.opts.metric;
  const commits = includedCommits();
  const keys = bucketRange(commits, gran);
  const idx = new Map(keys.map((k, i) => [k, i]));
  const mfn = (g) => (metric === "commits" ? g.commits : g.add + g.del);
  const { top, rest } = topGroups(8, mfn);
  const restKeys = new Set(rest.map((g) => g.key));
  const series = new Map(top.map((g) => [g.key, new Array(keys.length).fill(0)]));
  const others = new Array(keys.length).fill(0);
  for (const c of commits) {
    const i = idx.get(bucketKey(localDate(c), gran));
    const val = metric === "commits" ? 1 : c.add + c.del;
    for (const k of commitPeople(c)) {
      if (series.has(k)) series.get(k)[i] += val;
      else if (restKeys.has(k)) others[i] += val;
    }
  }
  const datasets = top.map((g) => ({ label: g.name, data: series.get(g.key), backgroundColor: g.color }));
  if (rest.length) datasets.push({ label: `${rest.length} others`, data: others, backgroundColor: OTHERS });
  chart("#chart-activity", {
    type: "bar",
    data: { labels: keys, datasets },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" }, tooltip: { mode: "index", callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.raw)}` } } },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { maxTicksLimit: 24, autoSkip: true } }, y: { stacked: true, ticks: { callback: (v) => fmt(v), precision: 0 }, grid: { color: color("--line") } } },
    },
  });
}

function renderCumulative() {
  const commits = includedCommits();
  const gran = commits.length > 2500 ? "month" : state.opts.gran === "year" ? "month" : state.opts.gran;
  const keys = bucketRange(commits, gran);
  const idx = new Map(keys.map((k, i) => [k, i]));
  const { top } = topGroups(8, (g) => g.add + g.del);
  const running = new Map(top.map((g) => [g.key, 0]));
  const series = new Map(top.map((g) => [g.key, new Array(keys.length).fill(null)]));
  const total = new Array(keys.length).fill(null);
  let totalRun = 0;
  for (const c of commits) {
    const key = canonical(state.data.authors[c.a].key);
    const g = state.groupByKey.get(key);
    if (g?.hidden) continue;
    const i = idx.get(bucketKey(localDate(c), gran));
    totalRun += c.add - c.del; total[i] = totalRun;
    if (running.has(key)) { running.set(key, running.get(key) + c.add - c.del); series.get(key)[i] = running.get(key); }
  }
  // carry forward so lines do not drop to nothing in quiet weeks
  const fill = (arr) => { let last = 0; for (let i = 0; i < arr.length; i++) { if (arr[i] == null) arr[i] = last; else last = arr[i]; } return arr; };
  const datasets = top.map((g) => ({ label: g.name, data: fill(series.get(g.key)), borderColor: g.color, backgroundColor: g.color, borderWidth: 2, pointRadius: 0, tension: 0 }));
  datasets.push({ label: "everyone", data: fill(total), borderColor: color("--text"), borderWidth: 1.5, borderDash: [4, 4], pointRadius: 0, tension: 0 });
  chart("#chart-cumulative", {
    type: "line",
    data: { labels: keys, datasets },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom" }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.raw)}` } } },
      scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 18, autoSkip: true } }, y: { ticks: { callback: (v) => fmt(v), precision: 0 }, grid: { color: color("--line") } } },
    },
  });
}

function renderHeatmap() {
  const grid = Array.from({ length: 7 }, () => new Array(24).fill(0));
  let max = 0;
  for (const c of includedCommits()) {
    const g = state.groupByKey.get(canonical(state.data.authors[c.a].key));
    if (g?.hidden) continue;
    const d = localDate(c);
    const day = (d.getUTCDay() + 6) % 7; // Monday first
    const v = ++grid[day][d.getUTCHours()];
    if (v > max) max = v;
  }
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const accent = color("--accent");
  let html = `<div></div>` + Array.from({ length: 24 }, (_, h) => `<div class="hm-hour">${h % 3 === 0 ? h : ""}</div>`).join("");
  grid.forEach((row, d) => {
    html += `<div class="hm-label">${names[d]}</div>`;
    row.forEach((v, h) => {
      const a = max ? 0.12 + 0.88 * Math.pow(v / max, 0.6) : 0;
      html += `<div class="hm-cell" data-tip="${names[d]} ${String(h).padStart(2, "0")}:00 · ${fmt(v)} commits" style="${v ? `background:color-mix(in srgb, ${accent} ${Math.round(a * 100)}%, var(--panel-2))` : ""}"></div>`;
    });
  });
  $("#heatmap").innerHTML = html;
}

function renderTypes() {
  const owned = state.opts.ftm === "owned";
  if (owned && !state.ownership) { $("#wrap-types").innerHTML = `<p class="hint" style="padding:30px 10px">Compute ownership first to see which file types make up today's code.</p>`; return; }
  if (!$("#chart-types")) $("#wrap-types").innerHTML = `<canvas id="chart-types"></canvas>`;
  const totals = {};
  for (const g of visibleGroups()) {
    const src = owned ? g.ownedExt : g.ext;
    for (const [ext, v] of Object.entries(src)) totals[ext] = (totals[ext] || 0) + (owned ? v : v[0]);
  }
  const entries = Object.entries(totals).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const top = entries.slice(0, 14), restSum = entries.slice(14).reduce((s, [, n]) => s + n, 0);
  const labels = top.map(([e]) => e), data = top.map(([, n]) => n);
  if (restSum) { labels.push(`${entries.length - 14} other types`); data.push(restSum); }
  $("#wrap-types").style.height = `${Math.max(220, 40 + labels.length * 22)}px`;
  chart("#chart-types", {
    type: "bar",
    data: { labels, datasets: [{ data, backgroundColor: owned ? color("--own") : color("--add") }] },
    options: {
      indexAxis: "y", maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${fmt(c.raw)} lines` } } },
      scales: { x: { ticks: { callback: (v) => fmt(v), precision: 0 }, grid: { color: color("--line") } }, y: { grid: { display: false }, ticks: { font: { family: color("--mono") } } } },
    },
  });
}

function renderTopFiles() {
  $("#top-files tbody").innerHTML = state.data.top_files.slice(0, 15).map((f) =>
    `<tr><td class="path" title="${esc(f.path)}">${esc(f.path)}</td><td class="num">${fmt(f.commits)}</td><td class="num add">+${fmt(f.add)}</td><td class="num del">−${fmt(f.del)}</td></tr>`).join("") || `<tr><td colspan="4" class="hint">Nothing to show.</td></tr>`;
}

function renderTopDays() {
  const days = new Map();
  for (const c of includedCommits()) {
    const g = state.groupByKey.get(canonical(state.data.authors[c.a].key));
    if (g?.hidden) continue;
    const k = dayKey(localDate(c));
    const d = days.get(k) || { n: 0, add: 0, del: 0 };
    d.n++; d.add += c.add; d.del += c.del; days.set(k, d);
  }
  const top = [...days.entries()].sort((a, b) => b[1].n - a[1].n || (b[1].add + b[1].del) - (a[1].add + a[1].del)).slice(0, 15);
  $("#top-days tbody").innerHTML = top.map(([k, d]) => `<tr><td>${k} <span class="hint" style="display:inline">${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(k + "T00:00:00Z").getUTCDay()]}</span></td><td class="num">${fmt(d.n)}</td><td class="num add">+${fmt(d.add)}</td><td class="num del">−${fmt(d.del)}</td></tr>`).join("") || `<tr><td colspan="4" class="hint">Nothing to show.</td></tr>`;
}

function renderCommitFilters() {
  const sel = $("#commit-person");
  const cur = sel.value;
  sel.innerHTML = `<option value="">Everyone</option>` + sortedGroups(visibleGroups(), "commits").map((g) => `<option value="${esc(g.key)}">${esc(g.name)}</option>`).join("");
  sel.value = state.groupByKey.has(cur) && !state.groupByKey.get(cur).hidden ? cur : "";
}
function filteredCommits() {
  const person = $("#commit-person").value;
  const term = $("#commit-search").value.trim().toLowerCase();
  const hideMerges = $("#commit-hide-merges").checked;
  const out = [];
  const commits = state.data.commits;
  for (let i = commits.length - 1; i >= 0; i--) {
    const c = commits[i];
    if (hideMerges && c.m) continue;
    if (!state.opts.merges && c.m) continue;
    const people = commitPeople(c);
    if (person) { if (!people.has(person)) continue; }
    else if ([...people].every((k) => state.groupByKey.get(k)?.hidden)) continue;
    if (term && !c.s.toLowerCase().includes(term) && !c.h.startsWith(term)) continue;
    out.push(c);
  }
  return out;
}
function renderCommits() {
  const rows = filteredCommits();
  const shown = rows.slice(0, state.commitsShown);
  $("#commit-count").textContent = `${fmt(rows.length)} commits`;
  $("#commits tbody").innerHTML = shown.map((c) => {
    const g = state.groupByKey.get(canonical(state.data.authors[c.a].key));
    const co = state.opts.coauthors ? c.c.map((ci) => state.groupByKey.get(canonical(state.data.authors[ci].key))).filter((x) => x && x.key !== g.key) : [];
    const when = localDate(c).toISOString().slice(0, 16).replace("T", " ");
    return `<tr>
      <td class="mono">${esc(c.h.slice(0, 7))}</td>
      <td class="mono" style="white-space:nowrap">${when}</td>
      <td><span class="who"><i class="dot" style="background:${g.color}"></i>${esc(g.name)}${co.map((x) => ` <span class="hint" style="display:inline" title="co-author">+ ${esc(x.name)}</span>`).join("")}</span></td>
      <td title="${esc(c.s)}">${esc(c.s)}${c.m ? `<span class="merge-tag">merge</span>` : ""}</td>
      <td class="num add">${c.add ? `+${fmt(c.add)}` : ""}</td>
      <td class="num del">${c.del ? `−${fmt(c.del)}` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="hint">No commits match.</td></tr>`;
  $("#btn-more-commits").hidden = rows.length <= state.commitsShown;
}

/* ------------------------------------------------------------------ browse modal */

let browseState = { path: "", isRepo: false };
async function openBrowse(path) {
  const modal = $("#browse-modal");
  modal.hidden = false;
  $("#browse-list").innerHTML = `<li class="empty">Loading…</li>`;
  try {
    const res = await api(`/api/browse?${q({ path: path ?? "" })}`);
    browseState = { path: res.path, isRepo: res.is_repo };
    const parts = res.path.split("/").filter(Boolean);
    let acc = "";
    $("#browse-crumbs").innerHTML = `<button data-path="/">${esc(state.config?.host_root || "repos")}</button>` + parts.map((p) => { acc += "/" + p; return `<span class="sep">/</span><button data-path="${esc(acc)}">${esc(p)}</button>`; }).join("");
    $("#browse-list").innerHTML = res.entries.map((e) => `<li class="${e.is_repo ? "repo" : ""}">
        <button class="name" data-path="${esc(e.path)}">${esc(e.name)}</button>
        ${e.is_repo ? `<span class="badge">git</span><button class="btn btn-xs" data-select="${esc(e.path)}">Analyze</button>` : ""}
      </li>`).join("") || `<li class="empty">No folders here.</li>`;
    $("#browse-hint").textContent = res.truncated ? "Showing the first 500 folders." : (res.is_repo ? "This folder is a git repository." : "Folders marked git can be analysed.");
    $("#browse-select").hidden = !res.is_repo;
  } catch (err) {
    $("#browse-list").innerHTML = `<li class="empty">${esc(err.message)}</li>`;
    $("#browse-select").hidden = true;
  }
}
function closeBrowse() { $("#browse-modal").hidden = true; }
$("#browse-modal").addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) return closeBrowse();
  const sel = e.target.closest("[data-select]");
  if (sel) { closeBrowse(); openRepo(sel.dataset.select); return; }
  const nav = e.target.closest("[data-path]");
  if (nav) openBrowse(nav.dataset.path === "/" ? "" : nav.dataset.path);
});
$("#browse-select").addEventListener("click", () => { closeBrowse(); openRepo(browseState.path); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#browse-modal").hidden) closeBrowse();
  if (!$("#export-modal").hidden) closeExport();
});

/* ------------------------------------------------------------------ export
 * Selected panels are cloned into an off-screen layout, charts are redrawn at
 * high DPI, and the whole thing is rasterised through an SVG <foreignObject>
 * so the export looks exactly like the app. PNG saves the full strip; PDF
 * slices it into A4 pages at panel boundaries. No libraries involved. */

const EXPORT_SECTIONS = [
  { key: "summary", label: "Summary numbers", sel: "#summary" },
  { key: "lines", label: "Lines by person", sel: "#panel-lines" },
  { key: "own", label: "Who owns the code", sel: "#panel-own", need: () => !!state.ownership },
  { key: "activity", label: "Activity over time", sel: "#panel-activity" },
  { key: "cumulative", label: "Lines over time", sel: "#panel-cumulative" },
  { key: "heatmap", label: "When people commit", sel: "#panel-heatmap" },
  { key: "types", label: "File types", sel: "#panel-types" },
  { key: "topfiles", label: "Most changed files", sel: "#panel-topfiles" },
  { key: "topdays", label: "Busiest days", sel: "#panel-topdays" },
  { key: "commits", label: "Commit list (as filtered)", sel: "#panel-commits" },
];
const EXPORT_PAIRS = { lines: "own", heatmap: "types", topfiles: "topdays" }; // side-by-side when both picked
const A4 = { w: 595.28, h: 841.89 }; // points, portrait
const PAGE_MARGIN = 24;              // css px of vertical margin per PDF page

function exportPrefs(save) {
  if (save) localStorage.setItem("gitscope:export", JSON.stringify(save));
  try { return JSON.parse(localStorage.getItem("gitscope:export") || "{}"); } catch { return {}; }
}
function openExport() {
  const prefs = exportPrefs();
  $("#export-options").innerHTML = EXPORT_SECTIONS.map((s) => {
    const ok = !s.need || s.need();
    const checked = ok && (!prefs.sections || prefs.sections.includes(s.key));
    return `<li><label class="check"><input type="checkbox" value="${s.key}" ${checked ? "checked" : ""} ${ok ? "" : "disabled"}> ${s.label}${ok ? "" : ` <span class="hint" style="display:inline;margin:0">— compute ownership first</span>`}</label></li>`;
  }).join("");
  if (prefs.format) $("#export-format").value = prefs.format;
  if (prefs.scale) $("#export-scale").value = String(prefs.scale);
  $("#export-hint").textContent = "";
  $("#export-modal").hidden = false;
}
function closeExport() { $("#export-modal").hidden = true; }

function exportHeader() {
  const r = state.data.repo;
  const bits = [`${r.branch} @ ${r.head.slice(0, 8)}`, `${fmt(includedCommits().length)} commits`];
  if (state.opts.all) bits.push("all branches");
  if (state.opts.coauthors) bits.push("co-authors counted");
  if (!state.opts.merges) bits.push("merges excluded");
  if (state.opts.ignore.trim()) bits.push(`ignoring ${state.opts.ignore.trim()}`);
  const div = document.createElement("div");
  div.className = "export-head";
  div.innerHTML = `<div class="export-brand">${$(".brand svg").outerHTML}<span>gitscope</span></div>
    <h1>${esc(r.name)}</h1><p class="hint">${esc(bits.join(" · "))}</p>`;
  return div;
}
function exportClone(sec) {
  const node = $(sec.sel).cloneNode(true);
  $$("canvas", node).forEach((cv) => { cv.dataset.chart = "#" + cv.id; });
  $$("[hidden], button, input, select, textarea, .seg, .check", node).forEach((el) => el.remove());
  $$("[id]", node).forEach((el) => el.removeAttribute("id")); // no duplicate ids while measuring
  const head = $(".panel-head", node);
  const addHint = (t) => head && head.insertAdjacentHTML("beforeend", `<span class="hint">${esc(t)}</span>`);
  if (sec.key === "activity") addHint(`${state.opts.metric === "commits" ? "commits" : "lines changed"} · by ${state.opts.gran}`);
  if (sec.key === "types") addHint(state.opts.ftm === "churn" ? "lines added" : "lines owned today");
  if (sec.key === "commits") {
    const total = filteredCommits().length;
    const shown = Math.min($$("tbody tr", node).length, total);
    const hint = $(".panel-head .hint", node);
    if (hint) hint.textContent = total > shown ? `first ${fmt(shown)} of ${fmt(total)} commits` : `${fmt(total)} commits`;
  }
  return node;
}
function buildExportDom(keys) {
  const sel = new Set(keys);
  const root = document.createElement("div");
  root.className = "export-root";
  root.appendChild(exportHeader());
  const done = new Set();
  for (const sec of EXPORT_SECTIONS) {
    if (!sel.has(sec.key) || done.has(sec.key)) continue;
    done.add(sec.key);
    const node = exportClone(sec);
    const partner = EXPORT_PAIRS[sec.key];
    if (partner && sel.has(partner)) {
      done.add(partner);
      const row = document.createElement("div");
      row.className = "grid-2";
      row.append(node, exportClone(EXPORT_SECTIONS.find((s) => s.key === partner)));
      root.appendChild(row);
    } else root.appendChild(node);
  }
  const foot = document.createElement("div");
  foot.className = "export-foot";
  foot.innerHTML = `<span>${esc(state.repoPath)}</span><span>generated by gitscope · ${new Date().toISOString().slice(0, 10)}</span>`;
  root.appendChild(foot);
  return root;
}
// Redraw every chart at its export size with devicePixelRatio = scale, swap the canvas for an <img>.
function snapshotCharts(root, scale) {
  for (const cv of $$("canvas", root)) {
    const cfg = chartCfgs[cv.dataset.chart];
    const wrap = cv.parentElement;
    const img = document.createElement("img");
    img.style.cssText = "display:block;width:100%;height:100%";
    if (cfg) {
      const off = document.createElement("canvas");
      off.width = wrap.clientWidth; off.height = wrap.clientHeight;
      const c = new Chart(off, { ...cfg, options: { ...cfg.options, responsive: false, animation: false, devicePixelRatio: scale } });
      img.src = off.toDataURL("image/png");
      c.destroy();
    }
    cv.replaceWith(img);
  }
}
let exportCssCache = null;
async function exportCss() {
  if (exportCssCache == null) {
    // :root becomes .export-root so the theme variables apply inside the SVG document
    exportCssCache = (await (await fetch("/style.css")).text()).replaceAll(":root", ".export-root");
  }
  return exportCssCache.replace(/&/g, "&amp;").replace(/</g, "&lt;");
}
async function rasterize(root, wCss, hCss, scale) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.round(wCss * scale)}" height="${Math.round(hCss * scale)}" viewBox="0 0 ${wCss} ${hCss}">` +
    `<style>${await exportCss()}</style><foreignObject width="${wCss}" height="${hCss}">${new XMLSerializer().serializeToString(root)}</foreignObject></svg>`;
  try {
    const img = new Image();
    // A data: URL, not a blob: one — Chrome taints canvases drawn from blob-url foreignObject SVGs.
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    await img.decode();
    return img;
  } catch {
    throw new Error("the browser could not render the export image");
  }
}
// Page break candidates: start a new page rather than cutting a panel in half.
function pageBreaks(root, contentH) {
  const breaks = [0];
  let start = 0;
  for (const el of Array.from(root.children)) {
    const bottom = el.offsetTop + el.offsetHeight;
    const top = Math.max(0, el.offsetTop - 8);
    if (bottom - start > contentH && top > start) { start = top; breaks.push(top); }
  }
  return breaks;
}
function slicePages(totalH, breaks, contentH) {
  const pages = [];
  for (let i = 0; i < breaks.length; i++) {
    const end = i + 1 < breaks.length ? breaks[i + 1] : totalH;
    for (let y = breaks[i]; y < end; y += contentH) pages.push({ y, h: Math.min(contentH, end - y) });
  }
  return pages;
}
async function pageJpeg(img, wCss, pageH, page, scale) {
  const c = document.createElement("canvas");
  c.width = Math.round(wCss * scale); c.height = Math.round(pageH * scale);
  const ctx = c.getContext("2d");
  ctx.fillStyle = color("--bg"); ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(img, 0, Math.round(page.y * scale), c.width, Math.round(page.h * scale), 0, Math.round(PAGE_MARGIN * scale), c.width, Math.round(page.h * scale));
  const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.92));
  return new Uint8Array(await blob.arrayBuffer());
}
// Minimal PDF: one full-page JPEG per page. Offsets are byte-exact, nothing fancy.
function buildPdf(jpegs, imgW, imgH) {
  const enc = new TextEncoder();
  const parts = [];
  const offsets = [0];
  let pos = 0;
  const push = (d) => { const u = typeof d === "string" ? enc.encode(d) : d; parts.push(u); pos += u.length; };
  const begin = () => offsets.push(pos);
  push("%PDF-1.4\n%µ¶µ¶\n");
  begin(); push(`1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n`);
  begin(); push(`2 0 obj\n<< /Type /Pages /Kids [${jpegs.map((_, i) => `${5 + 3 * i} 0 R`).join(" ")}] /Count ${jpegs.length} >>\nendobj\n`);
  jpegs.forEach((jpg, i) => {
    const content = `q ${A4.w} 0 0 ${A4.h} 0 0 cm /Im Do Q`;
    begin(); push(`${3 + 3 * i} 0 obj\n<< /Type /XObject /Subtype /Image /Width ${imgW} /Height ${imgH} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpg.length} >>\nstream\n`); push(jpg); push(`\nendstream\nendobj\n`);
    begin(); push(`${4 + 3 * i} 0 obj\n<< /Length ${content.length} >>\nstream\n${content}\nendstream\nendobj\n`);
    begin(); push(`${5 + 3 * i} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${A4.w} ${A4.h}] /Resources << /XObject << /Im ${3 + 3 * i} 0 R >> >> /Contents ${4 + 3 * i} 0 R >>\nendobj\n`);
  });
  const xref = pos;
  push(`xref\n0 ${offsets.length}\n0000000000 65535 f \n` + offsets.slice(1).map((o) => `${String(o).padStart(10, "0")} 00000 n \n`).join(""));
  push(`trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
  return new Blob(parts, { type: "application/pdf" });
}
function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}
async function runExport() {
  const keys = $$("#export-options input:checked").map((i) => i.value);
  if (!keys.length) { $("#export-hint").textContent = "Pick at least one section."; return; }
  const format = $("#export-format").value;
  let scale = Number($("#export-scale").value) || 2;
  exportPrefs({ sections: keys, format, scale });
  closeExport();
  loading("Rendering export…");
  const stage = document.createElement("div");
  stage.style.cssText = "position:fixed;left:-100000px;top:0;pointer-events:none";
  try {
    const root = buildExportDom(keys);
    stage.appendChild(root);
    document.body.appendChild(stage);
    const wCss = root.offsetWidth, hCss = root.offsetHeight;
    if (format === "png" && hCss * scale > 30000) scale = Math.max(1, Math.floor(30000 / hCss)); // canvas size ceiling
    snapshotCharts(root, scale);
    const img = await rasterize(root, wCss, hCss, scale);
    const name = `${state.data.repo.name}-gitscope-${new Date().toISOString().slice(0, 10)}.${format}`;
    if (format === "png") {
      const c = document.createElement("canvas");
      c.width = Math.round(wCss * scale); c.height = Math.round(hCss * scale);
      const ctx = c.getContext("2d");
      ctx.fillStyle = color("--bg"); ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0);
      const blob = await new Promise((r) => c.toBlob(r, "image/png"));
      if (!blob) throw new Error("the image is too large for the browser; try a lower resolution");
      download(blob, name);
    } else {
      const pageH = wCss * (A4.h / A4.w);
      const contentH = pageH - 2 * PAGE_MARGIN;
      const pages = slicePages(hCss, pageBreaks(root, contentH), contentH);
      const jpegs = [];
      for (const p of pages) jpegs.push(await pageJpeg(img, wCss, pageH, p, scale));
      download(buildPdf(jpegs, Math.round(wCss * scale), Math.round(pageH * scale)), name);
    }
    notice(`Exported ${keys.length} section${keys.length === 1 ? "" : "s"} to ${name}.`, "info");
  } catch (err) {
    notice(`Export failed: ${err.message}`);
  } finally {
    stage.remove();
    loading(null);
  }
}
$("#btn-export").addEventListener("click", openExport);
$("#export-run").addEventListener("click", runExport);
$("#export-all").addEventListener("click", () => $$("#export-options input:not(:disabled)").forEach((i) => { i.checked = true; }));
$("#export-none").addEventListener("click", () => $$("#export-options input").forEach((i) => { i.checked = false; }));
$("#export-modal").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeExport(); });

/* ------------------------------------------------------------------ wiring */

$("#repo-form").addEventListener("submit", (e) => { e.preventDefault(); openRepo($("#repo-path").value); });
$("#btn-browse").addEventListener("click", () => openBrowse(""));
$("#btn-browse-welcome").addEventListener("click", () => openBrowse(""));
$("#btn-ownership").addEventListener("click", startOwnership);
$("#btn-more-commits").addEventListener("click", () => { state.commitsShown += 200; renderCommits(); });
$("#commit-search").addEventListener("input", () => { state.commitsShown = 100; renderCommits(); });
$("#commit-person").addEventListener("change", () => { state.commitsShown = 100; renderCommits(); });
$("#commit-hide-merges").addEventListener("change", () => { state.commitsShown = 100; renderCommits(); });
$("#roster-sort").addEventListener("change", (e) => { state.opts.sort = e.target.value; saveLocalOpts(); renderRoster(); });
$("#opt-coauthors").addEventListener("change", (e) => { state.opts.coauthors = e.target.checked; saveLocalOpts(); renderAll(); });
$("#opt-merges").addEventListener("change", (e) => { state.opts.merges = e.target.checked; saveLocalOpts(); renderAll(); });
$("#opt-all").addEventListener("change", (e) => { state.opts.all = e.target.checked; openRepo(state.repoPath, { reread: true }); });
$("#opt-ignore").addEventListener("change", (e) => { if (e.target.value === state.opts.ignore) return; state.opts.ignore = e.target.value; openRepo(state.repoPath, { reread: true }); });
$("#btn-reanalyze").addEventListener("click", () => openRepo(state.repoPath, { reread: true }));
$$("[data-metric]").forEach((b) => b.addEventListener("click", () => { state.opts.metric = b.dataset.metric; $$("[data-metric]").forEach((x) => x.classList.toggle("on", x === b)); saveLocalOpts(); renderActivity(); }));
$$("[data-gran]").forEach((b) => b.addEventListener("click", () => { state.opts.gran = b.dataset.gran; $$("[data-gran]").forEach((x) => x.classList.toggle("on", x === b)); saveLocalOpts(); renderActivity(); renderCumulative(); }));
$$("[data-ftm]").forEach((b) => b.addEventListener("click", () => { state.opts.ftm = b.dataset.ftm; $$("[data-ftm]").forEach((x) => x.classList.toggle("on", x === b)); saveLocalOpts(); renderTypes(); }));

async function boot() {
  try {
    state.config = await api("/api/config");
    $("#welcome-root").textContent = state.config.host_root || state.config.repos_root;
    if (!state.config.root_mounted) notice(`Nothing is mounted at ${state.config.repos_root}. Set REPOS_ROOT in your .env file and restart the container.`);
    const recent = await api("/api/recent");
    if (recent.length) {
      $("#recent-wrap").hidden = false;
      $("#recent").innerHTML = recent.map((r) => `<li><button class="btn" data-open="${esc(r.path)}"><span>${esc(r.name)}</span><span>${esc(r.path)}</span></button></li>`).join("");
      $("#recent").addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b) openRepo(b.dataset.open); });
    }
  } catch (err) { notice(`Backend not reachable: ${err.message}`); }
  const fromUrl = new URLSearchParams(location.search).get("repo");
  if (fromUrl) openRepo(fromUrl);
}
boot();
