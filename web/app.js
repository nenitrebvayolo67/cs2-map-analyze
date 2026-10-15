"use strict";
const $ = (s) => document.querySelector(s);
const COLORS = ["#ff7a45", "#4cc3ff", "#b48cff", "#5ce0a0", "#ffd166", "#ff6b9a", "#9ad94a", "#e0e0e0"];
const ECON = { all: "Vše", pistol: "Pistolová", eco: "Eco", force: "Force", full: "Full-buy", unknown: "Neznámá" };
const WEIGHT_TXT = {
  rounds: "Podíl kol: každé kolo hráče má váhu 1, rozdělenou podle času stráveného v oblastech. 20 s na jednom místě = pořád jen 1 kolo.",
  matches: "Podíl zápasů: každý zápas hráče má váhu 1, takže zápas s více koly nepřebije ostatní.",
  time: "Čas strávený: každá sekunda živého hráče má stejnou váhu (zvýhodňuje dlouhé stání).",
};
const RUNNING = new Set(["loading", "players", "history", "downloading", "parsing", "rendering"]);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const st = {
  job: null, snap: null, view: null, dataVersion: -1, viewKey: "", polling: null, renderReported: false,
  f: { side: "T", interval: "all", mode: "heat", weight: "rounds", econ: "all", level: "0" },
  hidden: new Set(), color: {}, radar: { 0: null, 1: null }, intervals: [],
};

// ---------------------------------------------------------------- init
async function init() {
  const s = await fetch("/api/status").then((r) => r.json());
  st.intervals = s.intervals;
  const iv = $("#intervals");
  iv.append(btn("all", "Celé kolo"));
  s.intervals.forEach((k) => iv.append(btn(k, k + " s")));
  const miss = [];
  if (!s.api_key) miss.push("<code>FACEIT_API_KEY</code>");
  if (!s.identity) miss.push("<code>FACEIT_NICKNAME</code>");
  if (miss.length) showBanner(`V souboru <code>.env</code> chybí ${miss.join(" a ")}. Spusť <code>SETUP.bat</code> nebo soubor doplň a aplikaci restartuj.`);
  else if (!s.download_token) showBanner("Bez <code>FACEIT_DOWNLOAD_TOKEN</code> nelze dema stahovat automaticky. Aplikace ti přesně ukáže, která dema chybí. Přístup k Downloads API schvaluje FACEIT.");
  document.querySelectorAll("[data-key]").forEach((g) => {
    g.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (!b) return;
      st.f[g.dataset.key] = b.dataset.v;
      syncControls();
      if (g.dataset.key === "mode") draw(); else loadView();
    });
  });
  syncControls();
  $("#form").addEventListener("submit", start);
  $("#stepsToggle").addEventListener("click", () => {
    const el = $("#steps"); el.hidden = !el.hidden;
    $("#stepsToggle").setAttribute("aria-expanded", String(!el.hidden));
  });
  setupHover();
  if (s.latest_job) attach(s.latest_job);
}
function btn(v, label) { const b = document.createElement("button"); b.dataset.v = v; b.textContent = label; return b; }
function showBanner(html) { const b = $("#setup"); b.innerHTML = html; b.hidden = false; }
function syncControls() {
  document.querySelectorAll("[data-key]").forEach((g) => {
    g.querySelectorAll("button[data-v]").forEach((b) => b.setAttribute("aria-pressed", String(st.f[g.dataset.key] === b.dataset.v)));
  });
  $("#explain").textContent = WEIGHT_TXT[st.f.weight] + (st.f.mode === "dots" ? " Body: 1 tečka = 1 kolo v intervalu (pozice nejblíže jeho středu)." : "");
  $("#shareHead").textContent = { rounds: "Podíl kol", matches: "Podíl zápasů", time: "Podíl času" }[st.f.weight];
}

async function start(e) {
  e.preventDefault();
  const url = $("#url").value.trim();
  $("#go").disabled = true;
  try {
    const r = await fetch("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    const d = await r.json();
    if (!r.ok) { setStatus("error", d.error || "Chyba"); return; }
    st.view = null; st.dataVersion = -1; st.renderReported = false; st.radar = { 0: null, 1: null }; st.hidden.clear();
    attach(d.job);
  } finally { $("#go").disabled = false; }
}

function attach(id) {
  st.job = id;
  $("#intro").hidden = true; $("#app").hidden = false;
  clearTimeout(st.polling);
  poll();
}

async function poll() {
  let snap;
  try {
    const r = await fetch(`/api/job/${st.job}`);
    if (!r.ok) { setStatus("error", (await r.json()).error || "Analýza nenalezena"); return; }
    snap = await r.json();
  } catch { st.polling = setTimeout(poll, 2000); return; }
  st.snap = snap;
  renderSnapshot(snap);
  if (snap.map_ready && !st.radar[0]) loadRadar(snap);
  if (snap.data_version !== st.dataVersion && snap.data_version > 0) { st.dataVersion = snap.data_version; loadView(); }
  if (!snap.final) st.polling = setTimeout(poll, snap.state === "waiting_veto" ? 3000 : 1000);
}

function setStatus(state, text) {
  const el = $("#status");
  el.dataset.state = RUNNING.has(state) ? "running" : state;
  $("#statusText").textContent = text;
}

// ---------------------------------------------------------------- snapshot
function renderSnapshot(s) {
  const label = s.state === "done" ? "Hotovo" : s.state_label;
  setStatus(s.state, label);
  $("#mapName").textContent = s.map ? s.map.replace(/^de_/, "") : (s.state === "waiting_veto" ? "čeká na veto" : "—");
  $("#myTeam").textContent = s.my_team || "—";
  $("#oppTeam").textContent = s.opp_team || "—";
  $("#message").textContent = s.message || "";
  $("#levelGroup").hidden = !s.has_lower;
  const w = $("#warnings");
  w.hidden = !s.warnings.length;
  w.innerHTML = s.warnings.map((x) => `<div>${esc(x)}</div>`).join("");
  $("#steps").innerHTML = Object.values(s.steps).map((x) =>
    `<div class="step" data-s="${esc(x.status)}"><div class="n">${esc(x.label)}</div><div class="v">${
      x.seconds != null ? x.seconds.toFixed(x.seconds < 10 ? 2 : 1) + " s" : ({ running: "běží…", pending: "—", skipped: "přeskočeno", error: "chyba" }[x.status] || "—")}</div></div>`).join("");
  s.opponents.forEach((o, i) => { st.color[o.steam_id || o.player_id] = COLORS[i % COLORS.length]; });
  renderPlayers(s);
  renderCoverage(s);
  updateEmpty();
}

function renderPlayers(s) {
  const box = $("#players");
  box.innerHTML = "";
  s.opponents.forEach((o) => {
    const key = o.steam_id || o.player_id;
    const c = st.color[key];
    const b = document.createElement("button");
    b.className = "pcard"; b.type = "button"; b.style.setProperty("--c", c);
    b.setAttribute("aria-pressed", String(!st.hidden.has(key)));
    b.title = "Kliknutím hráče skryješ / zobrazíš";
    const av = o.avatar && /^https:\/\//.test(o.avatar)
      ? `<img class="av" src="${esc(o.avatar)}" alt="" referrerpolicy="no-referrer">`
      : `<span class="av ph">${esc((o.nickname || "?")[0].toUpperCase())}</span>`;
    const sm = o.summary;
    const d = o.demos || {};
    const onMap = o.on_map ?? 0;
    const pct = (n) => (onMap ? (n / onMap) * 100 : 0);
    b.innerHTML = `<div class="head">${av}<span class="nick">${esc(o.nickname)}</span></div>
      <div class="stats"><span><b>${o.history_found ?? "…"}</b>/${o.history_target} zápasů</span>
        <span><b>${o.on_map ?? "…"}</b> na mapě</span><span><b>${d.parsed ?? 0}</b> zpracováno</span></div>
      <div class="stats"><span>T <b>${sm ? sm.rounds_T : 0}</b> kol</span><span>CT <b>${sm ? sm.rounds_CT : 0}</b> kol</span></div>
      <div class="bar" title="zpracováno / chybí / chyba"><i style="width:${pct(d.parsed || 0)}%;background:${c}"></i>
        <i style="width:${pct((d.unavailable || 0) + (d.not_in_demo || 0))}%;background:#e8b04a66"></i><i style="width:${pct(d.error || 0)}%;background:#ef5b5b88"></i></div>
      ${o.verified_steam ? "" : '<span class="flag" title="Bez ověřeného Steam ID">⚠</span>'}`;
    const img = b.querySelector("img");
    if (img) img.addEventListener("error", () => img.replaceWith(Object.assign(document.createElement("span"), { className: "av ph", textContent: (o.nickname || "?")[0].toUpperCase() })));
    b.addEventListener("click", () => {
      st.hidden.has(key) ? st.hidden.delete(key) : st.hidden.add(key);
      if (st.hidden.size === s.opponents.length) st.hidden.delete(key);
      renderPlayers(st.snap); loadView();
    });
    box.append(b);
  });
}

function renderCoverage(s) {
  $("#coverage").innerHTML = s.opponents.map((o) => {
    const d = o.demos || {};
    const ln = (k, v, bad) => `<div class="ln${bad ? " bad" : ""}"><span>${k}</span><b>${v}</b></div>`;
    const reasons = Object.entries(o.missing_reasons || {}).filter(([r]) => r).map(([r, n]) => `${n}× ${esc(r)}`).join("<br>");
    const avail = (o.on_map ?? 0) - (d.unavailable || 0);
    return `<div class="cov"><div class="nm">${esc(o.nickname)}</div>
      ${ln(`Posledních ${o.history_target} zápasů nalezeno`, o.history_found ?? "…", o.history_found != null && o.history_found < o.history_target)}
      ${ln("Na vybrané mapě", o.on_map ?? "…")}
      ${o.map_unknown ? ln("Mapu nešlo ověřit", o.map_unknown, true) : ""}
      ${ln("Dema dostupná", o.on_map == null ? "…" : avail, avail < (o.on_map ?? 0))}
      ${ln("Dema zpracovaná", d.parsed ?? 0, (d.parsed ?? 0) < avail)}
      ${d.pending ? ln("Zpracovává se", d.pending) : ""}
      ${d.error ? ln("Chyba zpracování", d.error, true) : ""}
      ${d.not_in_demo ? ln("Hráč v demu nenalezen", d.not_in_demo, true) : ""}
      ${ln("Analýza vychází z", (d.parsed ?? 0) + " zápasů")}
      ${reasons ? `<div class="reason">${reasons}</div>` : ""}</div>`;
  }).join("");
  const stLabel = { parsed: "zpracováno", unavailable: "nedostupné", error: "chyba", pending: "čeká", downloading: "stahuje se", parsing: "parsuje se" };
  $("#matchList").innerHTML = s.matches.map((m) => {
    const ok = m.state === "parsed", bad = m.state === "error" || m.state === "unavailable";
    const when = m.finished_at ? new Date(m.finished_at * 1000).toLocaleDateString("cs-CZ") : "";
    return `<div class="ml"><a href="https://www.faceit.com/cs/cs2/room/${esc(m.match_id)}" target="_blank" rel="noopener noreferrer">${esc(m.match_id.slice(0, 13))}… ${when}</a>
      <span>${esc((m.players || []).join(", "))}${m.reason ? ` — <span class="muted">${esc(m.reason)}</span>` : ""}</span>
      <span class="st ${ok ? "ok" : bad ? "bad" : ""}">${stLabel[m.state] || esc(m.state)}</span></div>`;
  }).join("") || '<div class="muted" style="padding:6px 0">Zatím žádné.</div>';
}

// ---------------------------------------------------------------- data
function loadRadar(snap) {
  [0, 1].forEach((lv) => {
    if (lv === 1 && !snap.has_lower) return;
    const im = new Image();
    im.onload = () => { st.radar[lv] = im; draw(); };
    im.src = `/api/job/${st.job}/radar?lower=${lv}`;
  });
}

async function loadView() {
  if (!st.job || !st.snap || st.snap.data_version < 1) return;
  const visible = st.snap.opponents.map((o) => o.steam_id).filter((k) => k && !st.hidden.has(k));
  const q = new URLSearchParams({ players: visible.join(","), side: st.f.side, interval: st.f.interval, econ: st.f.econ, weight: st.f.weight, level: st.f.level });
  const key = q.toString() + "|" + st.dataVersion;
  st.viewKey = key;
  const r = await fetch(`/api/job/${st.job}/view?${q}`);
  if (!r.ok || st.viewKey !== key) return;
  st.view = await r.json();
  renderStats();
  draw();
}

function renderStats() {
  const v = st.view, t = v.totals;
  const box = (k, val) => `<div><div class="k">${k}</div><div class="v">${val}</div></div>`;
  $("#totals").innerHTML = box("Zápasů", t.matches) + box("Kol s výskytem", t.rounds) + box("Pozic", t.samples.toLocaleString("cs-CZ"))
    + (v.filters.interval !== "all"
      ? box("Živý na začátku", t.alive_at_start) + box("Eliminován dříve", t.eliminated_before) + box("Kolo už skončilo", t.round_over_before)
      : box("Kol odehráno", t.rounds_played));
  $("#areas").innerHTML = v.areas.slice(0, 14).map((a) =>
    `<tr><td class="barcell"><i style="width:${Math.min(100, a.share)}%"></i><span>${esc(a.place)}</span></td><td class="r">${a.share.toFixed(1)} %</td><td class="r">${a.rounds}</td></tr>`).join("")
    || '<tr><td colspan="3" class="muted">Žádná data pro tento filtr.</td></tr>';
  updateEmpty();
}

function updateEmpty() {
  const s = st.snap, el = $("#empty");
  let msg = "";
  if (!s) msg = "";
  else if (s.state === "waiting_veto") msg = "Čekám na konec veta. Jakmile bude vybraná mapa, analýza pokračuje sama.";
  else if (!s.map) msg = s.state === "error" ? "" : "Načítám zápas…";
  else if (s.final && !s.map_ready) msg = "Chybí ověřené mapové podklady, proto nelze pozice vykreslit.";
  else if (!st.view && s.final) msg = "Pro tuto mapu nejsou k dispozici žádná zpracovaná dema. Podrobnosti najdeš níže v části „Co bylo ověřeno“.";
  else if (!st.view) msg = "Zpracovávám dema…";
  else if (!st.view.totals.samples) msg = "Pro zvolený filtr nejsou žádné pozice.";
  if (s && s.state === "error") msg = s.error || "Chyba";
  el.hidden = !msg; el.textContent = msg;
}

// ---------------------------------------------------------------- kreslení
const cv = $("#map"), ctx = cv.getContext("2d");
const PAL = (() => {
  const c = document.createElement("canvas"); c.width = 256; c.height = 1;
  const g = c.getContext("2d"), gr = g.createLinearGradient(0, 0, 256, 0);
  gr.addColorStop(0, "rgba(58,29,138,0)"); gr.addColorStop(0.12, "rgba(58,29,138,.55)");
  gr.addColorStop(0.4, "rgba(196,58,107,.78)"); gr.addColorStop(0.7, "rgba(255,154,60,.9)"); gr.addColorStop(1, "rgba(255,242,168,.97)");
  g.fillStyle = gr; g.fillRect(0, 0, 256, 1);
  return g.getImageData(0, 0, 256, 1).data;
})();

function blur(src, n, r) {
  const k = [], sig = r / 2; let sum = 0;
  for (let i = -r; i <= r; i++) { const v = Math.exp(-(i * i) / (2 * sig * sig)); k.push(v); sum += v; }
  for (let i = 0; i < k.length; i++) k[i] /= sum;
  const tmp = new Float32Array(n * n), out = new Float32Array(n * n);
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    let a = 0; for (let i = -r; i <= r; i++) { const xx = x + i; if (xx >= 0 && xx < n) a += src[y * n + xx] * k[i + r]; } tmp[y * n + x] = a;
  }
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    let a = 0; for (let i = -r; i <= r; i++) { const yy = y + i; if (yy >= 0 && yy < n) a += tmp[yy * n + x] * k[i + r]; } out[y * n + x] = a;
  }
  return out;
}

const heatCv = document.createElement("canvas");
function draw() {
  const t0 = performance.now();
  const S = 1024;
  ctx.clearRect(0, 0, S, S);
  const im = st.radar[Number(st.f.level)] || (st.f.level === "0" ? null : st.radar[0]);
  if (im) { ctx.drawImage(im, 0, 0, S, S); ctx.fillStyle = "rgba(11,12,15,.38)"; ctx.fillRect(0, 0, S, S); }
  else { ctx.fillStyle = "#0e1014"; ctx.fillRect(0, 0, S, S); }
  const v = st.view;
  drawLegend();
  if (!v) return;
  if (st.f.mode === "heat") {
    const n = S / v.heat_bin, grid = new Float32Array(n * n);
    for (const [x, y, w] of v.heat) grid[y * n + x] += w;
    const b = blur(grid, n, 4);
    let max = 0; for (const x of b) if (x > max) max = x;
    heatCv.width = heatCv.height = n;
    const hc = heatCv.getContext("2d"), id = hc.createImageData(n, n), d = id.data;
    for (let i = 0; i < b.length; i++) {
      const j = Math.min(255, Math.round(Math.sqrt(max ? b[i] / max : 0) * 255)) * 4;
      d[i * 4] = PAL[j]; d[i * 4 + 1] = PAL[j + 1]; d[i * 4 + 2] = PAL[j + 2]; d[i * 4 + 3] = PAL[j + 3];
    }
    hc.putImageData(id, 0, 0);
    ctx.imageSmoothingEnabled = true; ctx.drawImage(heatCv, 0, 0, S, S);
  } else {
    const r = v.dots.length > 800 ? 4 : 5.5;
    for (const [sid, , x, y] of v.dots) {
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = st.color[sid] || "#fff"; ctx.globalAlpha = 0.82; ctx.fill();
      ctx.globalAlpha = 1; ctx.lineWidth = 1.2; ctx.strokeStyle = "rgba(0,0,0,.7)"; ctx.stroke();
    }
  }
  if (!st.renderReported && st.snap && st.snap.final && v.totals.samples) {
    st.renderReported = true;
    const ms = performance.now() - t0;
    requestAnimationFrame(() => fetch(`/api/job/${st.job}/render`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ms }) }).then(poll));
  }
}

function drawLegend() {
  const s = st.snap; if (!s) return;
  const pl = s.opponents.filter((o) => o.steam_id && !st.hidden.has(o.steam_id))
    .map((o) => `<span><span class="sw" style="background:${st.color[o.steam_id]}"></span>${esc(o.nickname)}</span>`).join("");
  $("#legend").innerHTML = st.f.mode === "heat"
    ? `<span>Méně</span><span class="grad"></span><span>Více</span><span class="muted">· ${esc(ECON[st.f.econ])} · ${st.f.side === "ALL" ? "obě strany" : st.f.side}</span>`
    : pl;
}

function setupHover() {
  const tip = $("#tip");
  cv.addEventListener("mousemove", (e) => {
    const v = st.view; if (!v) { tip.hidden = true; return; }
    const rect = cv.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 1024, y = ((e.clientY - rect.top) / rect.height) * 1024;
    const c = v.cells[`${Math.floor(x / v.cell)},${Math.floor(y / v.cell)}`];
    if (!c) { tip.hidden = true; return; }
    const names = c.players.map((sid) => (st.snap.opponents.find((o) => o.steam_id === sid) || {}).nickname || "?");
    tip.innerHTML = `<div class="pl">${esc(c.place || "Oblast")}</div>
      <div class="row"><span>${esc($("#shareHead").textContent)}</span><b>${c.share.toFixed(1)} %</b></div>
      <div class="row"><span>Kol s výskytem</span><b>${c.rounds}</b></div>
      <div class="row"><span>Zaznamenaných pozic</span><b>${c.samples}</b></div>
      <div class="row"><span>Hráči</span><b>${esc(names.join(", "))}</b></div>`;
    tip.hidden = false;
    const wr = $("#canvasWrap").getBoundingClientRect();
    let left = e.clientX - wr.left + 14, top = e.clientY - wr.top + 14;
    if (left + 240 > wr.width) left -= 260;
    if (top + 120 > wr.height) top -= 130;
    tip.style.left = left + "px"; tip.style.top = top + "px";
  });
  cv.addEventListener("mouseleave", () => { tip.hidden = true; });
}

init();
