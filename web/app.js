/* wpe-studio front end. Vanilla, no build step, no CDN.
 *
 * State lives on the server; this file is a renderer plus a thin command layer.
 * Anything that changes the machine goes through a POST and then re-reads, so
 * the UI can never show a setting the daemon does not actually have.
 */
"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const App = {
  boot: null,
  items: [],
  sel: null,
  detail: null,
  monitor: null,
  filters: { q: "", type: new Set(), source: new Set(), rating: new Set(), tags: new Set() },
  sort: "recent",
  ws: { page: 1, sort: "trend", q: "" },
  scripted: new Set(),   // wallpapers driven by their own scene script
  selecting: false,
  picked: new Set(),
  plPicked: new Set(),
};

/* ── api ──────────────────────────────────────────────────────────── */
// When the daemon is bound off loopback it requires a token. It arrives in the
// page URL (?t=...); every call carries it back so a bookmarked link keeps
// working without a login screen.
const API_TOKEN = new URLSearchParams(location.search).get("t") || "";
const withToken = (p) =>
  !API_TOKEN ? p : p + (p.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(API_TOKEN);

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (API_TOKEN) headers["Authorization"] = "Bearer " + API_TOKEN;
  const opt = body === undefined
    ? (API_TOKEN ? { headers } : {})
    : { method: "POST", headers, body: JSON.stringify(body) };
  const r = await fetch(withToken(path), opt);
  const text = await r.text();
  let j;
  try { j = JSON.parse(text); } catch { j = { error: text.slice(0, 400) }; }
  if (!r.ok && !j.error) j.error = "HTTP " + r.status;
  return j;
}

let toastTimer = null;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", !!bad);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), bad ? 6000 : 2600);
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ── boot ─────────────────────────────────────────────────────────── */
async function boot() {
  const b = await api("/api/bootstrap");
  if (b.error) { toast("Daemon unreachable: " + b.error, true); return; }
  App.boot = b;
  App.items = b.library;
  document.body.dataset.skin = b.state.global.skin || "dark";
  const a = (b.state.global.opacity ?? 100) / 100;
  document.documentElement.style.setProperty("--alpha", a.toFixed(2));
  document.documentElement.style.setProperty("--blur", a < 1 ? "18px" : "0px");
  App.monitor = App.monitor || (b.monitors[0] && b.monitors[0].name);

  renderCapabilities();
  renderFilters();
  renderGrid();
  renderMonitors();
  renderSettings();
  renderPlaylists();
  renderStatus();
  if (App.sel) openDetail(App.sel);
}

/* ── the embedded Steam view ──────────────────────────────────────────
 * When we are running inside the app window there is a real WebKit view
 * behind this page that can show steamcommunity.com signed in, so the
 * Workshop tab hands over to it. In a plain browser tab that bridge does not
 * exist, and the tab falls back to the scraped grid.                        */
const HOST = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.wpe;
const hostSend = (msg) => { try { HOST.postMessage(JSON.stringify(msg)); } catch { } };
const embedded = !!HOST;

function showSteam(show, url, navigate) {
  if (!embedded) return false;
  hostSend({ action: "steam", show, url, navigate });
  return true;
}
window.__wpeLeaveSteam = () => {
  const t = $('.tab[data-view="installed"]');
  if (t) t.click();
};
window.__wpeRescan = () => { setTimeout(() => $("#btnRescan").click(), 1500); };

/* ── environment ──────────────────────────────────────────────────── */
// Most of this only works on X11, and the desktop-integration bits are per
// desktop environment. Say so where it matters instead of quietly no-opping.
function renderCapabilities() {
  const c = (App.boot.status || {}).capabilities;
  if (!c) return;
  const host = $("#envnote");
  const problems = [];
  if (!c.render) problems.push(c.render_note);
  if (!c.background) problems.push(
    `Setting the lock screen background is not supported on ${c.desktop}.`);
  if (!c.desktop_icons) problems.push(
    `The desktop-icons toggle is not supported on ${c.desktop} — if icons cover `
    + `the wallpaper, turn them off in your file manager's settings.`);
  if (App.boot.docker) problems.push(
    "Running in Docker: the Steam launch hook, desktop icons and autostart act "
    + "on the container, so they are disabled.");
  if (!problems.length) { host.innerHTML = ""; host.hidden = true; return; }
  host.hidden = false;
  host.innerHTML = `<div class="warnbox"><b>${esc(c.desktop)} / ${esc(c.session)}</b> —
    tested on ${esc(c.tested_on)}.<ul style="margin:6px 0 0 16px;padding:0">
    ${problems.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>`;
}

/* ── tabs ─────────────────────────────────────────────────────────── */
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (!b) return;
  $$(".tab").forEach((t) => t.classList.toggle("active", t === b));
  $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === b.dataset.view));
  const workshop = b.dataset.view === "workshop";
  showSteam(workshop);
  if (workshop && !embedded && !$("#wsGrid").children.length) loadWorkshop();
});

/* ── filters ──────────────────────────────────────────────────────── */
function counts(key) {
  const m = new Map();
  for (const it of App.items) {
    const vals = key === "tags" ? it.tags : [it[key]];
    for (const v of vals || []) {
      if (v == null || v === "") continue;
      m.set(v, (m.get(v) || 0) + 1);
    }
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])));
}

function checkList(host, key, entries) {
  host.innerHTML = entries.map(([v, n]) => `
    <label><input type="checkbox" data-k="${esc(key)}" value="${esc(v)}"
      ${App.filters[key].has(String(v)) ? "checked" : ""}>
      <span>${esc(v)}</span><span class="n">${n}</span></label>`).join("");
}

function renderFilters() {
  checkList($("#fType"), "type", counts("type"));
  checkList($("#fSource"), "source", counts("source"));
  checkList($("#fRating"), "rating", counts("contentrating"));
  checkList($("#fTags"), "tags", counts("tags"));
}

document.addEventListener("change", (e) => {
  const cb = e.target.closest(".checks input[type=checkbox]");
  if (!cb) return;
  const set = App.filters[cb.dataset.k];
  cb.checked ? set.add(cb.value) : set.delete(cb.value);
  renderGrid();
});

$("#search").addEventListener("input", (e) => { App.filters.q = e.target.value; renderGrid(); });
$("#btnResetFilters").addEventListener("click", () => {
  App.filters = { q: "", type: new Set(), source: new Set(), rating: new Set(), tags: new Set() };
  $("#search").value = "";
  renderFilters();
  renderGrid();
});
$("#sortSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  App.sort = b.dataset.sort;
  $$("#sortSeg button").forEach((x) => x.classList.toggle("active", x === b));
  renderGrid();
});
const hb = $("#hideBroken");
if (hb) hb.addEventListener("change", (e) => { App.hideBroken = e.target.checked; renderGrid(); });
$("#zoom").addEventListener("input", (e) => {
  document.documentElement.style.setProperty("--tilew", e.target.value + "px");
});

function filtered() {
  const f = App.filters;
  const q = f.q.trim().toLowerCase();
  let out = App.items.filter((it) => {
    if (q && !(it.title.toLowerCase().includes(q) || it.id.includes(q)
               || (it.tags || []).some((t) => t.toLowerCase().includes(q)))) return false;
    if (f.type.size && !f.type.has(it.type)) return false;
    if (f.source.size && !f.source.has(it.source)) return false;
    if (f.rating.size && !f.rating.has(it.contentrating)) return false;
    if (f.tags.size && !(it.tags || []).some((t) => f.tags.has(t))) return false;
    if (App.hideBroken && (App.boot.state.incompatible || {})[it.id]) return false;
    return true;
  });
  const favs = new Set(App.boot.state.favorites || []);
  const cmp = {
    recent: (a, b) => b.mtime - a.mtime,
    name: (a, b) => a.title.localeCompare(b.title),
    type: (a, b) => a.type.localeCompare(b.type) || a.title.localeCompare(b.title),
    favorite: (a, b) => (favs.has(b.id) - favs.has(a.id)) || a.title.localeCompare(b.title),
  }[App.sort];
  return out.sort(cmp);
}

function runningIds() {
  const s = new Set();
  for (const m of (App.boot.status.monitors || [])) if (m.running && m.id) s.add(String(m.id));
  return s;
}

function tileHTML(it, live, fav) {
  const mature = it.contentrating && it.contentrating !== "Everyone";
  const bad = (App.boot.state.incompatible || {})[it.id];
  const preset = it.type === "preset";
  return `<div class="tile ${live ? "live" : ""} ${App.sel === it.id ? "selected" : ""} ${
      App.picked.has(it.id) ? "picked" : ""}" data-id="${esc(it.id)}">
    ${it.has_preview
      ? `<img loading="lazy" src="${withToken("/preview/" + encodeURIComponent(it.id))}" alt="">`
      : `<div class="noimg"><i class="fa">&#xf03e;</i></div>`}
    <div class="badges">
      <span class="badge type">${esc(it.type[0].toUpperCase() + it.type.slice(1))}</span>
      ${it.customizable ? `<span class="badge custom">${it.prop_count}</span>` : ""}
      ${live ? `<span class="badge running">Active</span>` : ""}
      ${preset ? `<span class="badge preset">Preset</span>` : ""}
      ${bad ? `<span class="badge broken" title="${esc(bad)}">Unsupported</span>` : ""}
      ${App.scripted.has(it.id) ? `<span class="badge scripted"
        title="Driven by a scene script — some of these respond to clicking the wallpaper">script</span>` : ""}
      ${mature ? `<span class="badge mature">${esc(it.contentrating)}</span>` : ""}
    </div>
    <div class="pick">${App.picked.has(it.id) ? "✓" : ""}</div>
    <div class="fav ${fav ? "on" : ""}" data-fav="${esc(it.id)}"><i class="fa">&#xf005;</i></div>
    <div class="cap">${esc(it.title)}</div>
  </div>`;
}

function renderGrid() {
  if (!App.boot) return;
  const live = runningIds();
  const favs = new Set(App.boot.state.favorites || []);
  const list = filtered();
  $("#grid").innerHTML = list.map((it) => tileHTML(it, live.has(it.id), favs.has(it.id))).join("")
    || `<div class="muted" style="padding:20px">No matches. ${App.items.length} installed.</div>`;
  $("#fcount").textContent = `(${list.length} of ${App.items.length})`;
}

$("#grid").addEventListener("click", async (e) => {
  const fav = e.target.closest("[data-fav]");
  if (fav) {
    e.stopPropagation();
    const r = await api("/api/favorite", { id: fav.dataset.fav });
    App.boot.state.favorites = r.favorites;
    fav.classList.toggle("on", r.favorites.includes(fav.dataset.fav));
    return;
  }
  const t = e.target.closest(".tile");
  if (!t) return;
  if (App.selecting) {
    const id = t.dataset.id;
    App.picked.has(id) ? App.picked.delete(id) : App.picked.add(id);
    renderGrid();
    renderSelbar();
    return;
  }
  openDetail(t.dataset.id);
});
// Double-click applies, the same as the Apply button. It also selects, so the
// properties panel is showing the wallpaper that just went up rather than
// whatever was open before.
$("#grid").addEventListener("dblclick", (e) => {
  const t = e.target.closest(".tile");
  if (!t || App.selecting) return;      // while selecting, a double click is two picks
  openDetail(t.dataset.id);
  applyWallpaper(t.dataset.id);
});

// Same in the playlist picker's grid and the walkthrough's, so the gesture
// means one thing everywhere.
$("#plGrid").addEventListener("dblclick", (e) => {
  const t = e.target.closest("[data-pl]");
  if (t) applyWallpaper(t.dataset.pl);
});

/* ── selection and deletion ───────────────────────────────────────── */
const fmtBytes = (n) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
};

function renderSelbar() {
  $("#selCount").textContent = App.picked.size + " selected";
  $("#selDelete").disabled = App.picked.size === 0;
  $("#selUnsub").disabled = App.picked.size === 0;
}

function setSelecting(on) {
  App.selecting = on;
  document.body.classList.toggle("selecting", on);
  $("#selbar").hidden = !on;
  $("#btnSelect").textContent = on ? "Done selecting" : "Select";
  if (!on) App.picked.clear();
  renderGrid();
  renderSelbar();
}

$("#btnSelect").onclick = () => setSelecting(!App.selecting);
$("#selNone").onclick = () => { App.picked.clear(); renderGrid(); renderSelbar(); };
$("#selAll").onclick = () => {
  filtered().forEach((it) => App.picked.add(it.id));
  renderGrid();
  renderSelbar();
};

// Unsubscribing is Steam's to do -- it owns the subscription, and deleting the
// folder behind its back just makes it download the wallpaper again. So this
// takes you to the page with the button on it.
$("#selUnsub").onclick = () => openUnsubscribe([...App.picked]);

function openUnsubscribe(ids) {
  if (!ids.length) return;
  if (ids.length === 1) {
    const url = "https://steamcommunity.com/sharedfiles/filedetails/?id=" + ids[0];
    if (embedded) {
      $('.tab[data-view="workshop"]').click();
      showSteam(true, url, true);
    } else {
      api("/api/steam", { action: "open_item", id: ids[0] });
    }
    toast("Hit Unsubscribe on the page, then Rescan");
    return;
  }
  const url = "https://steamcommunity.com/my/myworkshopfiles/?appid=431960&browsefilter=mysubscriptions";
  if (embedded) {
    $('.tab[data-view="workshop"]').click();
    showSteam(true, url, true);
  } else {
    api("/api/steam", { action: "open_url", url });
  }
  toast("Steam's subscriptions page — it can unsubscribe in bulk");
}

$("#selDelete").onclick = () => confirmDelete([...App.picked]);

async function confirmDelete(ids) {
  if (!ids.length) return;
  const p = await api("/api/delete/preview", { ids });
  if (p.error) return toast(p.error, true);

  const rows = p.items.map((x) => `
    <div class="cfrow ${x.blocked ? "blocked" : ""}">
      <span>${x.blocked ? "✕" : x.reason ? "!" : "•"}</span>
      <span style="flex:1;min-width:0">${esc(x.title)}
        ${x.reason ? `<br><small style="color:var(--fg3)">${esc(x.reason)}</small>` : ""}
        ${x.running ? `<br><small style="color:var(--orange)">currently on screen — it will be stopped</small>` : ""}
      </span>
      <span class="sz">${x.blocked ? "skipped" : fmtBytes(x.size)}</span>
    </div>`).join("");

  $("#cfTitle").textContent =
    p.deletable === 1 ? "Delete this wallpaper?" : `Delete ${p.deletable} wallpapers?`;
  $("#cfBody").innerHTML = `
    <p class="muted">Removes the files from disk and frees <b>${fmtBytes(p.bytes)}</b>.
    This does not unsubscribe — if you are still subscribed, Steam will download
    them again. Use <b>Unsubscribe in Steam</b> first if you want them gone for good.</p>
    <div class="cflist">${rows}</div>`;
  $("#cfOk").disabled = p.deletable === 0;
  $("#cfOk").textContent = p.deletable === 0 ? "Nothing to delete"
                                             : `Delete ${p.deletable}`;
  $("#confirm").hidden = false;

  $("#cfCancel").onclick = () => { $("#confirm").hidden = true; };
  $("#cfOk").onclick = async () => {
    $("#confirm").hidden = true;
    toast("Deleting…");
    const r = await api("/api/delete", { ids });
    if (r.items) App.items = r.items;
    const failed = (r.results || []).filter((x) => !x.ok);
    toast(failed.length ? `Freed ${fmtBytes(r.freed)}, ${failed.length} skipped`
                        : `Deleted — freed ${fmtBytes(r.freed)}`, !!failed.length);
    App.picked.clear();
    if (App.sel && !App.items.some((i) => i.id === App.sel)) {
      App.sel = null;
      App.detail = null;
      $("#detail").innerHTML = `<div class="empty">No wallpaper selected</div>`;
    }
    App.boot.state = await api("/api/state");
    renderFilters();
    renderGrid();
    renderSelbar();
  };
}

/* ── detail panel ─────────────────────────────────────────────────── */
// Update the grid IN PLACE rather than re-rendering it.
//
// renderGrid() replaces #grid.innerHTML, which destroys the tile you just
// clicked. The second click of a double click then lands on a brand new
// element, the browser never fires dblclick, and double-click-to-apply
// silently does nothing. Selecting a wallpaper must not rebuild the grid.
function markSelected(id) {
  for (const el of $$("#grid .tile")) {
    el.classList.toggle("selected", el.dataset.id === id);
  }
}

function refreshTileBadges(id) {
  const el = $(`#grid .tile[data-id="${CSS.escape(id)}"] .badges`);
  const it = App.items.find((x) => x.id === id);
  if (!el || !it) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = tileHTML(it, runningIds().has(id),
                           (App.boot.state.favorites || []).includes(id));
  const fresh = tmp.querySelector(".badges");
  if (fresh) el.innerHTML = fresh.innerHTML;
}

async function openDetail(id) {
  App.sel = id;
  markSelected(id);
  const d = await api("/api/wallpaper/" + encodeURIComponent(id));
  if (d.error) { toast(d.error, true); return; }
  App.detail = d;
  const had = App.scripted.has(id);
  if (d.script_driven) App.scripted.add(id); else App.scripted.delete(id);
  drawDetail();
  if (had !== App.scripted.has(id)) refreshTileBadges(id);
}

function fmtSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}

function drawDetail() {
  const d = App.detail;
  if (!d) return;
  const it = d.item;
  const live = runningIds().has(it.id);

  const bad = (App.boot.state.incompatible || {})[it.id];
  const head = `
    <h3>${esc(it.title)}</h3>
    <div class="byline">${esc(it.type)} · ${fmtSize(it.size)} · ${esc(it.source)}</div>
    ${it.has_preview ? `<img class="shot" src="${withToken("/preview/" + encodeURIComponent(it.id))}" alt="">` : ""}
    <div class="taglist">
      ${(it.tags || []).map((t) => `<span>${esc(t)}</span>`).join("")}
      ${it.contentrating ? `<span>${esc(it.contentrating)}</span>` : ""}
      ${it.customizable ? `<span>Customizable</span>` : ""}
    </div>
    <div class="btnrow one">
      <button class="btn primary" id="dApply"><i class="fa">&#xf04b;</i> ${live ? "Re-apply" : "Apply"}</button>
    </div>
    <div class="btnrow">
      <button class="btn" id="dFav">${d.favorite ? "★ Favorited" : "☆ Favorite"}</button>
      <button class="btn" id="dSteam">Steam page</button>
    </div>
    <div class="btnrow">
      <button class="btn" id="dUnsub">Unsubscribe</button>
      <button class="btn danger" id="dDelete">Delete</button>
    </div>
    ${it.type === "preset" ? `<div class="warnbox" style="font-size:11px">
        <b>Preset.</b> A saved option set for
        <b>${esc(it.dependency_title || it.dependency)}</b>${it.dependency_ok ? "" : " — not subscribed"}.
        Applying runs that wallpaper with these values.</div>` : ""}
    ${bad ? `<div class="warnbox" style="font-size:11px"><b>Unsupported.</b> Failed to start.
        <br><span style="font-family:monospace">${esc(bad.split("\n").slice(-2).join(" ")).slice(0, 240)}</span>
        <br>A linux-wallpaperengine limitation, not a setting.</div>` : ""}
    ${it.description ? `<div class="sect">Description</div><div class="desc">${esc(it.description)}</div>` : ""}
  `;

  const scripted = d.script_driven;
  // These used to be inert. The engine now runs the scene-script hooks
  // (applyUserProperties / cursorClick), so the note explains the behaviour
  // rather than apologising for it -- including that some of these wallpapers
  // are driven by CLICKING them, which is not discoverable otherwise.
  const scriptWarn = scripted ? `<div class="warnbox"
      style="font-size:11px;color:#cfe3ff;background:rgba(65,131,245,.10);border-color:var(--accent)">
      <b>Script-driven wallpaper.</b> Its options run through a scene script, which
      this build now executes. Two things worth knowing:
      <br>• Some of these react to being <b>clicked</b> rather than to a setting —
      if an option looks like it does nothing, try clicking the wallpaper.
      <br>• A few authors ship options they never wired up; those are dead in the
      wallpaper itself, not here.</div>` : "";

  const editable = d.properties.filter((p) => p.type !== "group" || true);
  const props = editable.length
    ? `<div class="sect">Properties</div>${scriptWarn}<div id="props">${editable.map(propHTML).join("")}</div>
       <div class="btnrow">
         <button class="btn" id="pReset"><i class="fa">&#xf2f1;</i> Reset</button>
         <button class="btn" id="pSave"><i class="fa">&#xf0c7;</i> Save preset</button>
       </div>
       ${d.presets.length ? `<div class="btnrow one"><select id="pLoad">
            <option value="">Load preset</option>
            ${d.presets.map((n) => `<option>${esc(n)}</option>`).join("")}
          </select></div>` : ""}`
    : `<div class="sect">Properties</div>${scriptWarn}<div class="muted">No configurable settings.</div>`;

  $("#detail").innerHTML = head + props;

  $("#dApply").onclick = () => applyWallpaper(it.id);
  $("#dFav").onclick = async () => {
    const r = await api("/api/favorite", { id: it.id });
    App.boot.state.favorites = r.favorites;
    d.favorite = r.favorites.includes(it.id);
    drawDetail(); renderGrid();
  };
  $("#dUnsub").onclick = () => openUnsubscribe([it.id]);
  $("#dDelete").onclick = () => confirmDelete([it.id]);
  $("#dSteam").onclick = () => {
    const wid = it.workshopid || it.id;
    const url = "https://steamcommunity.com/sharedfiles/filedetails/?id=" + wid;
    if (embedded) {
      $('.tab[data-view="workshop"]').click();
      showSteam(true, url, true);
      return;
    }
    api("/api/steam", { action: "open_item", id: wid });
  };
  const pr = $("#pReset");
  if (pr) pr.onclick = async () => {
    await api("/api/props/reset", { id: it.id });
    toast("Reset to author defaults");
    openDetail(it.id);
  };
  const ps = $("#pSave");
  if (ps) ps.onclick = async () => {
    const name = prompt("Preset name");
    if (!name) return;
    await api("/api/preset", { id: it.id, name, props: currentProps() });
    toast("Preset saved");
    openDetail(it.id);
  };
  const pl = $("#pLoad");
  if (pl) pl.onchange = async () => {
    if (!pl.value) return;
    await api("/api/preset/load", { id: it.id, name: pl.value });
    toast("Preset loaded");
    openDetail(it.id);
  };
  wireProps();
}

/* colours in project.json are "r g b" as floats 0..1 */
function floatsToHex(v) {
  const p = String(v == null ? "1 1 1" : v).trim().split(/\s+/).map(Number);
  const c = [0, 1, 2].map((i) => {
    const n = Math.round(Math.min(1, Math.max(0, p[i] || 0)) * 255);
    return n.toString(16).padStart(2, "0");
  });
  return "#" + c.join("");
}
function hexToFloats(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return "1 1 1";
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
    .map((x) => +(x / 255).toFixed(6)).join(" ");
}

function propHTML(p) {
  const hid = p.visible ? "" : " hidden";
  const k = esc(p.key);
  if (p.type === "group")
    return `<div class="prop group${hid}" data-key="${k}">${esc(p.label)}</div>`;
  if (p.type === "text")
    return `<div class="prop${hid}" data-key="${k}"><div class="muted">${esc(p.label)}</div></div>`;
  if (p.type === "bool")
    return `<div class="prop${hid}" data-key="${k}"><div class="rowline">
      <label style="flex:1;margin:0">${esc(p.label)}</label>
      <label class="switch"><input type="checkbox" data-p="${k}" ${p.value ? "checked" : ""}><span></span></label>
    </div></div>`;
  if (p.type === "slider") {
    const step = p.step || (p.fraction ? 0.01 : 1);
    return `<div class="prop${hid}" data-key="${k}"><label>${esc(p.label)}</label><div class="rowline">
      <input type="range" data-p="${k}" min="${p.min}" max="${p.max}" step="${step}" value="${p.value}">
      <output>${p.value}</output></div></div>`;
  }
  if (p.type === "color")
    // Show the hex, not the raw "0.0039 0.0039 0.0039" the format stores --
    // three long floats wrap onto three lines and read like a bug.
    return `<div class="prop${hid}" data-key="${k}"><label>${esc(p.label)}</label>
      <div class="swatchrow">
        <input type="color" data-p="${k}" data-kind="color" value="${floatsToHex(p.value)}">
        <span class="hex">${esc(floatsToHex(p.value).toUpperCase())}</span></div></div>`;
  if (p.type === "combo")
    return `<div class="prop${hid}" data-key="${k}"><label>${esc(p.label)}</label>
      <select data-p="${k}">${(p.options || []).map((o) =>
        `<option value="${esc(o.value)}" ${String(o.value) === String(p.value) ? "selected" : ""}>${esc(o.label)}</option>`).join("")}
      </select></div>`;
  if (p.type === "textinput")
    return `<div class="prop${hid}" data-key="${k}"><label>${esc(p.label)}</label>
      <input type="text" data-p="${k}" value="${esc(p.value)}"></div>`;
  return `<div class="prop${hid}" data-key="${k}"><label>${esc(p.label)}</label>
    <div class="note">${esc(p.type)} — not editable here${p.note ? " (" + esc(p.note) + ")" : ""}</div></div>`;
}

function currentProps() {
  const out = {};
  for (const el of $$("#props [data-p]")) {
    const key = el.dataset.p;
    if (el.type === "checkbox") out[key] = el.checked;
    else if (el.dataset.kind === "color") out[key] = hexToFloats(el.value);
    else if (el.type === "range") out[key] = parseFloat(el.value);
    else out[key] = el.value;
  }
  return out;
}

let propTimer = null;
function wireProps() {
  const host = $("#props");
  if (!host) return;
  host.addEventListener("input", (e) => {
    const el = e.target.closest("[data-p]");
    if (!el) return;
    lastEdit = Date.now();
    if (el.type === "range") {
      const o = el.parentElement.querySelector("output");
      if (o) o.textContent = el.value;
    }
    if (el.dataset.kind === "color") {
      const h = el.parentElement.querySelector(".hex");
      if (h) h.textContent = el.value.toUpperCase();
    }
    applyConditions();
    clearTimeout(propTimer);
    // Each push restarts the renderer, so a slider drag must not fire one per
    // pixel. Long enough to coalesce a drag, short enough to feel live.
    propTimer = setTimeout(pushProps, 700);
  });
}

/* Re-evaluate `condition` client-side so toggling a parent hides its children
 * immediately, the way the real app does, instead of on the next round trip. */
function applyConditions() {
  const vals = currentProps();
  for (const p of (App.detail.properties || [])) {
    const el = $(`#props .prop[data-key="${CSS.escape(p.key)}"]`);
    if (!el) continue;
    el.classList.toggle("hidden", !evalCond(p.condition, vals));
  }
}
function evalCond(cond, vals) {
  if (!cond) return true;
  let m = /^\s*([A-Za-z_]\w*)\s*(?:\.value\s*)?(===|==|!==|!=|>=|<=|>|<)\s*(true|false|-?\d+(?:\.\d+)?|'[^']*'|"[^"]*")\s*;?\s*$/.exec(cond);
  if (m) {
    const [, name, op, rawWant] = m;
    let want = rawWant === "true" ? true : rawWant === "false" ? false
      : /^['"]/.test(rawWant) ? rawWant.slice(1, -1) : parseFloat(rawWant);
    const cur = vals[name];
    const eq = typeof want === "boolean" ? Boolean(cur) === want
      : typeof want === "number" ? parseFloat(cur) === want : String(cur) === String(want);
    if (op === "==" || op === "===") return eq;
    if (op === "!=" || op === "!==") return !eq;
    const a = parseFloat(cur), b = parseFloat(want);
    return op === ">" ? a > b : op === "<" ? a < b : op === ">=" ? a >= b : a <= b;
  }
  m = /^\s*([A-Za-z_]\w*)(?:\.value)?\s*;?\s*$/.exec(cond);
  if (m) return Boolean(vals[m[1]] ?? true);
  return true;   // unparseable: show it. Losing a control is worse than an extra one.
}

async function pushProps() {
  if (!App.detail) return;
  const id = App.detail.item.id;
  const props = currentProps();
  const r = await api("/api/props", { id, props, apply: true });
  if (r.error) return toast(r.error, true);
  // Fold the change into the cached detail so a later redraw shows what was
  // actually sent rather than the values from when the panel was opened.
  App.detail.overrides = props;
  for (const p of App.detail.properties) {
    if (p.key in props) p.value = props[p.key];
  }
  if (r.reapplied) toast("Applied");
}

/* ── apply / stop ─────────────────────────────────────────────────── */
async function applyWallpaper(id) {
  toast("Starting");
  const r = await api("/api/apply", { id, monitor: App.monitor });
  if (!r.ok) {
    toast("Failed: " + (r.error || "unknown") + (r.log ? " — see Settings ▸ Engine log" : ""), true);
  } else if (r.warning) {
    toast(r.warning, true);
  } else {
    toast(r.title);
  }
  await refreshStatus();
}

$("#btnStop").onclick = async () => {
  await api("/api/stop", {});
  toast("Stopped");
  refreshStatus();
};

let lastLiveKey = "";

async function refreshStatus() {
  App.boot.status = await api("/api/status");
  App.boot.state = await api("/api/state");
  // Only rebuild the grid when something it displays actually changed.
  // Rebuilding on a 6s timer was destroying tiles mid-gesture.
  const liveKey = [...runningIds()].sort().join(",") + "|"
    + (App.boot.state.favorites || []).join(",") + "|" + App.items.length;
  if (liveKey !== lastLiveKey) {
    lastLiveKey = liveKey;
    renderGrid();
  }
  renderMonitors();
  renderStatus();
  // Never redraw the properties panel while it is being edited. drawDetail()
  // rebuilds every control from App.detail, which is the copy fetched when the
  // wallpaper was selected -- so a poll landing mid-edit put the OLD values
  // back in the DOM, and the next change pushed those old values to the
  // engine. That is what "it reverts after two seconds" was.
  if (App.detail && !editing()) drawDetail();
}

let lastEdit = 0;
const editing = () => Date.now() - lastEdit < 8000;

function renderMonitors() {
  const s = App.boot.status;
  $("#monitors").innerHTML = (s.monitors || []).map((m) => `
    <div class="mon ${m.monitor.name === App.monitor ? "active" : ""}" data-mon="${esc(m.monitor.name)}">
      <span>${esc(m.monitor.name)}</span>
      <small>${m.monitor.width}×${m.monitor.height}</small>
    </div>`).join("");
}
$("#monitors").addEventListener("click", (e) => {
  const m = e.target.closest("[data-mon]");
  if (!m) return;
  App.monitor = m.dataset.mon;
  renderMonitors();
  renderStatus();
});

function renderStatus() {
  const s = App.boot.status;
  const cur = (s.monitors || []).find((m) => m.monitor.name === App.monitor) || s.monitors[0];
  const g = App.boot.state.global;
  if (!cur) return;
  $("#nowplaying").innerHTML = cur.running
    ? `<span class="dot on"></span><b>${esc(cur.title || cur.id)}</b> · ${g.fps} fps · ${g.mute ? "muted" : g.volume + "% vol"}`
    : `<span class="dot"></span>Idle · ${esc(cur.monitor.name)}`;
}

/* ── settings ─────────────────────────────────────────────────────── */
function bindSetting(sel, key, kind) {
  const el = $(sel);
  const g = App.boot.state.global;
  if (kind === "bool") el.checked = !!g[key];
  else el.value = g[key];
  const out = $("#out" + key[0].toUpperCase() + key.slice(1));
  if (out) out.textContent = g[key];
  el.oninput = () => { if (out) out.textContent = el.value; };
  el.onchange = async () => {
    const v = kind === "bool" ? el.checked : (kind === "num" ? Number(el.value) : el.value);
    const r = await api("/api/settings", { global: { [key]: v } });
    App.boot.state.global = r.global;
    renderStatus();
  };
}

function renderSettings() {
  const g = App.boot.state.global;
  bindSetting("#setFps", "fps", "num");
  bindSetting("#setVolume", "volume", "num");
  bindSetting("#setMute", "mute", "bool");
  bindSetting("#setAutoMute", "auto_mute", "bool");
  bindSetting("#setAudioProc", "audio_processing", "bool");
  bindSetting("#setPause", "pause_on_fullscreen", "bool");
  bindSetting("#setScaling", "scaling");
  bindSetting("#setClamp", "clamp");
  bindSetting("#setParticles", "particles", "bool");
  bindSetting("#setMouse", "mouse", "bool");
  bindSetting("#setParallax", "parallax", "bool");

  const tp = $("#setTrayPanel");
  tp.checked = g.tray_panel !== false;
  tp.onchange = async () => {
    await api("/api/settings", { global: { tray_panel: tp.checked }, apply: false });
    App.boot.state.global.tray_panel = tp.checked;
    toast(tp.checked ? "Tray icon opens the panel" : "Tray icon opens a menu");
  };

  const op = $("#setOpacity");
  const applyOpacity = (v) => {
    document.documentElement.style.setProperty("--alpha", (v / 100).toFixed(2));
    // Blur only when there is something behind to blur; at full opacity it is
    // pure cost for no visible difference.
    document.documentElement.style.setProperty("--blur", v < 100 ? "18px" : "0px");
    $("#outOpacity").textContent = v + "%";
  };
  op.value = g.opacity ?? 100;
  applyOpacity(Number(op.value));
  op.oninput = () => applyOpacity(Number(op.value));
  op.onchange = async () => {
    await api("/api/settings", { global: { opacity: Number(op.value) }, apply: false });
    App.boot.state.global.opacity = Number(op.value);
  };

  const tr = $("#setTranslate");
  const trInfo = App.boot.translate || {};
  tr.checked = !!g.translate;
  tr.disabled = !trInfo.ok;
  $("#translateHint").textContent = trInfo.ok
    ? (trInfo.model_present
        ? `Uses ${trInfo.model} locally. Nothing leaves this machine; results are cached.`
        : `Ollama is running but ${trInfo.model} is not pulled — run: ollama pull ${trInfo.model}`)
    : `Needs Ollama running locally (${trInfo.error || "not reachable"}).`;
  tr.onchange = async () => {
    await api("/api/settings", { global: { translate: tr.checked }, apply: false });
    App.boot.state.global.translate = tr.checked;
    toast(tr.checked ? "Translating labels — first open of a wallpaper is slower"
                     : "Translation off");
    if (App.detail) openDetail(App.detail.item.id);
  };

  const skin = $("#setSkin");
  skin.innerHTML = App.boot.skins.map((s) =>
    `<option value="${s.key}" ${g.skin === s.key ? "selected" : ""}>${esc(s.label)}</option>`).join("");
  skin.onchange = async () => {
    document.body.dataset.skin = skin.value;
    await api("/api/settings", { global: { skin: skin.value }, apply: false });
  };

  const ast = $("#setAutostart");
  ast.checked = !!g.autostart;
  ast.onchange = async () => {
    await api("/api/autostart", { on: ast.checked });
    toast(ast.checked ? "Autostart on" : "Autostart off");
  };

  const icons = $("#setIcons");
  icons.checked = !!App.boot.status.desktop_icons;
  icons.onchange = async () => {
    const r = await api("/api/desktop-icons", { on: icons.checked });
    icons.checked = r.on;
    toast(r.on ? "Desktop icons on — these cover the wallpaper" : "Desktop icons off");
  };

  const hook = $("#setSteamHook");
  const st = App.boot.steam;
  hook.checked = !!st.installed;
  $("#steamHint").textContent = st.steam_running
    ? "Steam is running. Toggling closes Steam, writes the launch option, and reopens it."
    : (st.installed ? "Active. Play opens this app."
                    : "Inactive. Play launches the Windows build under Proton, which cannot render on Linux.");
  hook.onchange = async () => {
    const want = hook.checked;
    const running = App.boot.steam.steam_running;
    if (running && !confirm(
        "Steam rewrites localconfig.vdf on exit, so it must be closed for this "
        + "change to persist.\n\nClose Steam, apply, and reopen?")) {
      hook.checked = !want;
      return;
    }
    toast(running ? "Closing Steam" : "Writing launch option");
    if (running) await api("/api/steam", { action: "shutdown_steam_only" });
    const r = await api("/api/steam", { action: want ? "install" : "uninstall" });
    if (running) await api("/api/steam", { action: "start_steam" });
    if (r.error === "steam_running") toast(r.message || "Close Steam first", true);
    else if (!r.ok) toast(JSON.stringify(r.results || r), true);
    else toast(want ? "Play now opens this app" : "Launch option cleared");
    App.boot.steam = await api("/api/steam");
    renderSettings();
  };

  $("#btnSteamLib2").onclick = () => api("/api/steam", { action: "open_library" });
  $("#btnSteamWs").onclick = () => api("/api/steam", { action: "open_workshop" });
  $("#btnLog").onclick = async () => {
    const r = await api("/api/log?monitor=" + encodeURIComponent(App.monitor));
    $("#diag").textContent = r.log || "(empty)";
  };
  $("#btnWalkthrough").onclick = async () => {
    App.deps = await api("/api/deps");
    WT.i = 0;
    const el = $("#wt");
    el.hidden = false;
    el.style.display = "";
    wtRender();
  };
  $("#btnPaths").onclick = () => {
    $("#diag").textContent = JSON.stringify(
      { ...App.boot.paths, status: App.boot.status, saver: App.boot.saver }, null, 2);
  };
}

$("#btnSteamLib").onclick = () => api("/api/steam", { action: "open_library" });
if (embedded) {
  // The GTK Steam view covers this whole tab, so the scraped fallback grid and
  // its chrome would only ever be visible as a flash behind it.
  document.body.classList.add("embedded");
}
$("#btnRescan").onclick = async () => {
  const r = await api("/api/rescan", {});
  App.items = r.items;
  renderFilters();
  renderGrid();
  toast(App.items.length + " wallpapers");
};

/* ── workshop tab ─────────────────────────────────────────────────── */
async function loadWorkshop() {
  $("#wsGrid").innerHTML = `<div class="muted" style="padding:20px">Loading…</div>`;
  const r = await api(`/api/workshop?q=${encodeURIComponent(App.ws.q)}&sort=${App.ws.sort}&page=${App.ws.page}`);
  $("#wsPage").textContent = App.ws.page;
  if (!r.ok) {
    $("#wsGrid").innerHTML = `<div class="muted" style="padding:20px">Workshop unreachable: ${esc(r.error)}</div>`;
    return;
  }
  const installed = new Set(App.items.map((i) => i.id));
  $("#wsGrid").innerHTML = r.items.map((it) => `
    <div class="tile" data-ws="${esc(it.id)}">
      ${it.preview ? `<img loading="lazy" src="${esc(it.preview)}" alt="">` : `<div class="noimg"><i class="fa">&#xf03e;</i></div>`}
      <div class="badges">${installed.has(it.id) ? `<span class="badge custom">installed</span>` : ""}
        ${it.stars ? `<span class="badge">${it.stars}★</span>` : ""}</div>
      <div class="cap">${esc(it.title)}</div>
    </div>`).join("") || `<div class="muted" style="padding:20px">No results.</div>`;
}
$("#wsGrid").addEventListener("click", (e) => {
  const t = e.target.closest("[data-ws]");
  if (!t) return;
  const url = "https://steamcommunity.com/sharedfiles/filedetails/?id=" + t.dataset.ws;
  if (showSteam(true, url, true)) return;
  api("/api/steam", { action: "open_item", id: t.dataset.ws });
  toast("Opened in Steam");
});
$("#wsSearch").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  App.ws.q = e.target.value; App.ws.page = 1; loadWorkshop();
});
$("#wsSort").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#wsSort button").forEach((x) => x.classList.toggle("active", x === b));
  App.ws.sort = b.dataset.sort; App.ws.page = 1; loadWorkshop();
});
$("#wsPrev").onclick = () => { if (App.ws.page > 1) { App.ws.page--; loadWorkshop(); } };
$("#wsNext").onclick = () => { App.ws.page++; loadWorkshop(); };
$("#wsOpenSteam").onclick = () => api("/api/steam", { action: "open_workshop" });

/* ── playlists ────────────────────────────────────────────────────── */
function renderPlaylists() {
  const pls = App.boot.state.playlists || {};
  const active = App.boot.state.active_playlist;
  $("#plGrid").innerHTML = App.items.map((it) => `
    <div class="tile ${App.plPicked.has(it.id) ? "selected" : ""}" data-pl="${esc(it.id)}">
      ${it.has_preview ? `<img loading="lazy" src="${withToken("/preview/" + encodeURIComponent(it.id))}" alt="">`
                       : `<div class="noimg"><i class="fa">&#xf03e;</i></div>`}
      <div class="cap">${esc(it.title)}</div>
    </div>`).join("");
  $("#plList").innerHTML = Object.entries(pls).map(([name, pl]) => `
    <div class="plrow ${active === name ? "active" : ""}">
      <b>${esc(name)}</b>
      <span class="muted">${pl.items.length} wallpapers · every ${pl.interval_min} min · ${esc(pl.order)}</span>
      <span class="spacer" style="flex:1"></span>
      <button class="btn subtle" data-plact="${esc(name)}">${active === name ? "Stop" : "Start"}</button>
      <button class="btn subtle" data-pledit="${esc(name)}">Edit</button>
      <button class="btn danger" data-pldel="${esc(name)}">Delete</button>
    </div>`).join("") || `<div class="muted">No playlists yet.</div>`;
}
$("#plGrid").addEventListener("click", (e) => {
  const t = e.target.closest("[data-pl]");
  if (!t) return;
  const id = t.dataset.pl;
  App.plPicked.has(id) ? App.plPicked.delete(id) : App.plPicked.add(id);
  t.classList.toggle("selected");
});
$("#plSave").onclick = async () => {
  const name = $("#plName").value.trim();
  if (!name) return toast("Enter a name", true);
  if (!App.plPicked.size) return toast("Select at least one wallpaper", true);
  const r = await api("/api/playlist", {
    action: "save", name, items: [...App.plPicked],
    interval_min: Number($("#plInterval").value), order: $("#plOrder").value,
  });
  App.boot.state.playlists = r.playlists;
  toast("Saved");
  renderPlaylists();
};
$("#plList").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.plact) {
    const on = App.boot.state.active_playlist === b.dataset.plact;
    const r = await api("/api/playlist", { action: on ? "deactivate" : "activate", name: b.dataset.plact });
    App.boot.state.active_playlist = r.active;
  } else if (b.dataset.pldel) {
    const r = await api("/api/playlist", { action: "delete", name: b.dataset.pldel });
    App.boot.state.playlists = r.playlists;
    App.boot.state.active_playlist = r.active;
  } else if (b.dataset.pledit) {
    const pl = App.boot.state.playlists[b.dataset.pledit];
    $("#plName").value = b.dataset.pledit;
    $("#plInterval").value = pl.interval_min;
    $("#plOrder").value = pl.order;
    App.plPicked = new Set(pl.items);
  }
  renderPlaylists();
});

/* ── first-run walkthrough ───────────────────────────────────────────
 * Shown once, after the first successful boot. It exists because the two
 * things most likely to make this look broken on a fresh install are silent:
 * desktop icons covering the wallpaper, and Steam's Play button still opening
 * the Proton build. Both are checked here with a button to fix them, rather
 * than documented somewhere nobody reads.
 */
const WT = {
  i: 0,
  steps: [
    {
      title: "Wallpaper Engine, on Linux",
      body: () => `<p>This runs the wallpapers you already own on Steam, using
        <b>linux-wallpaperengine</b> as the renderer. Wallpaper Engine's own app
        cannot draw on a Linux desktop — that is the whole reason this exists.</p>
        <p>Four quick checks and you are done.</p>`,
    },
    {
      title: "Checking your setup",
      // Reads /etc/os-release and names the packages for THIS distro, so there
      // is one install command instead of a README with five sections.
      body: () => {
        const c = (App.boot.status || {}).capabilities || {};
        const st = App.boot.status || {};
        const steam = c.steam || {};
        const d = App.deps;
        const row = (state, what, detail, extra) =>
          `<div class="wtrow ${state}"><span class="ico">${
            state === "ok" ? "✔" : state === "warn" ? "!" : "✕"}</span>
            <span class="what">${esc(what)}<small>${esc(detail)}</small></span>${extra || ""}</div>`;

        let out = "";
        if (d) {
          out += `<p>Detected <b>${esc(d.distro.name)}</b>${
            d.distro.manager ? ` — packages via <b>${esc(d.distro.manager)}</b>` : ""}.</p>`;
          for (const r of d.requirements) {
            if (r.ok) continue;
            out += row(r.optional ? "warn" : "bad", r.what,
              r.packaged ? `${r.why} — package: ${r.package}`
                         : `${r.why} — not packaged for ${d.distro.manager || "this distro"}`);
          }
          if (d.install_command) {
            out += `<div class="wtrow warn"><span class="ico">↓</span>
              <span class="what">Install what is missing
                <small style="font-family:monospace">sudo ${esc(d.install_command)}</small></span>
              <button class="btn primary" id="wtInstall">Install</button></div>`;
          }
          if (!d.engine.ok) {
            out += row("bad", "linux-wallpaperengine", d.engine.note || "not found");
          }
          if (!d.missing.length && d.engine.ok) {
            out += row("ok", "Dependencies", "everything this needs is installed");
          }
        }
        out += row(steam.workshop_present ? "ok" : "bad", "Steam library",
                   steam.workshop_present ? steam.root
                     : "no Workshop content for app 431960 — subscribe to a wallpaper in Steam");
        out += row(App.items.length ? "ok" : "bad", "Wallpapers", App.items.length + " found");
        out += row(c.render ? "ok" : "bad", "Display server",
                   c.render ? c.session + " — supported" : (c.render_note || "unsupported"));
        return out;
      },
    },
    {
      title: "Desktop icons",
      body: () => {
        const on = App.boot.status.desktop_icons;
        return `<p>Your file manager owns the desktop's root window. While it does,
          it paints over the wallpaper and you get a black screen with a working
          process behind it — <b>the single most common reason this looks broken</b>.</p>
          <div class="wtrow ${on ? "warn" : "ok"}">
            <span class="ico">${on ? "!" : "✔"}</span>
            <span class="what">Desktop icons are ${on ? "ON" : "off"}
              <small>${on ? "They will cover the wallpaper." : "Nothing is covering the wallpaper."}</small></span>
            ${on ? `<button class="btn primary" id="wtIcons">Turn off</button>` : ""}
          </div>`;
      },
    },
    {
      title: "Pick a wallpaper",
      body: () => `<p>Click one to put it on your desktop now. You can change it any
        time from the grid, the tray icon, or <code>wpe-apply</code>.</p>
        <div class="wtgrid" id="wtPick">${App.items.slice(0, 12).map((it) =>
          `<div class="tile" data-wt="${esc(it.id)}">
            ${it.has_preview ? `<img loading="lazy" src="${withToken("/preview/" + encodeURIComponent(it.id))}" alt="">`
                             : `<div class="noimg"><i class="fa">&#xf03e;</i></div>`}
            <div class="cap">${esc(it.title)}</div></div>`).join("")}</div>`,
    },
    {
      title: "Start with your session",
      body: () => {
        const g = App.boot.state.global;
        const steam = App.boot.steam || {};
        return `<p>Two optional bits of wiring.</p>
          <div class="wtrow ${g.autostart ? "ok" : "warn"}">
            <span class="ico">${g.autostart ? "✔" : "○"}</span>
            <span class="what">Restore the wallpaper at login
              <small>Runs <code>wpe-apply --restore</code> when you log in.</small></span>
            <label class="switch"><input type="checkbox" id="wtAuto" ${g.autostart ? "checked" : ""}><span></span></label>
          </div>
          <div class="wtrow ${steam.installed ? "ok" : "warn"}">
            <span class="ico">${steam.installed ? "✔" : "○"}</span>
            <span class="what">Steam's Play button opens this app
              <small>${steam.installed ? "Active."
                : "Otherwise Play starts the Windows build under Proton, which cannot render."}
                ${steam.steam_running ? "Steam must be closed to change this." : ""}</small></span>
            ${steam.installed || steam.steam_running ? "" :
              `<button class="btn primary" id="wtSteam">Enable</button>`}
          </div>`;
      },
    },
    {
      title: "That's it",
      body: () => `<p>A few things worth knowing:</p>
        <div class="wtrow"><span class="ico">◆</span><span class="what">Some wallpapers react to clicks
          <small>Ones marked <b>script</b> on the tile run their own logic — several switch
          state when you click the wallpaper rather than through a setting.</small></span></div>
        <div class="wtrow"><span class="ico">◆</span><span class="what">Labels in another language
          <small>Settings ▸ Translate labels to English, done locally.</small></span></div>
        <div class="wtrow"><span class="ico">◆</span><span class="what">It keeps running without this window
          <small>Use the tray icon, or close this — the wallpaper stays up.</small></span></div>`,
    },
  ],
};

function wtRender() {
  const step = WT.steps[WT.i];
  $("#wtSteps").innerHTML = WT.steps.map((_s, n) =>
    `<i class="${n < WT.i ? "done" : n === WT.i ? "now" : ""}"></i>`).join("");
  $("#wtTitle").textContent = step.title;
  $("#wtText").innerHTML = "";
  $("#wtCheck").innerHTML = step.body();
  $("#wtBack").disabled = WT.i === 0;
  $("#wtNext").textContent = WT.i === WT.steps.length - 1 ? "Done" : "Next";

  const inst = $("#wtInstall");
  if (inst) inst.onclick = async () => {
    const r = await api("/api/install-deps", {});
    toast(r.popped ? "Root terminal opened — enter your password there"
                   : (r.error || "failed"), !r.popped);
  };

  const icons = $("#wtIcons");
  if (icons) icons.onclick = async () => {
    await api("/api/desktop-icons", { on: false });
    App.boot.status = await api("/api/status");
    wtRender();
  };
  const pick = $("#wtPick");
  if (pick) pick.onclick = async (e) => {
    const t = e.target.closest("[data-wt]");
    if (!t) return;
    $$("#wtPick .tile").forEach((x) => x.classList.remove("selected"));
    t.classList.add("selected");
    toast("Applying…");
    const r = await api("/api/apply", { id: t.dataset.wt });
    toast(r.ok ? "Applied — " + r.title : (r.error || "failed"), !r.ok);
  };
  const auto = $("#wtAuto");
  if (auto) auto.onchange = async () => {
    await api("/api/autostart", { on: auto.checked });
    App.boot.state.global.autostart = auto.checked;
  };
  const steam = $("#wtSteam");
  if (steam) steam.onclick = async () => {
    const r = await api("/api/steam", { action: "install" });
    App.boot.steam = await api("/api/steam");
    toast(r.ok ? "Play now opens this app" : (r.message || r.error || "failed"), !r.ok);
    wtRender();
  };
}

async function wtFinish() {
  // Close first and unconditionally: if the POST fails the user must still get
  // their app back, and the worst case is the walkthrough offering itself once
  // more next launch.
  const el = $("#wt");
  el.hidden = true;
  el.style.display = "none";
  try {
    await api("/api/walkthrough-done", {});
    App.boot.state.global.seen_walkthrough = true;
  } catch (e) {
    toast("Could not save that you finished setup", true);
  }
}

$("#wtNext").onclick = () => {
  if (WT.i === WT.steps.length - 1) return wtFinish();
  WT.i++; wtRender();
};
$("#wtBack").onclick = () => { if (WT.i > 0) { WT.i--; wtRender(); } };
$("#wtSkip").onclick = wtFinish;

async function maybeWalkthrough() {
  App.deps = await api("/api/deps");
  if (App.boot.state.global.seen_walkthrough) return;
  WT.i = 0;
  const el = $("#wt");
  el.hidden = false;
  el.style.display = "";
  wtRender();
}

/* ── go ───────────────────────────────────────────────────────────── */
/* #workshop / #settings in the URL opens straight to that tab -- handy for a
 * launcher shortcut and for driving the app from a script. */
function openHashTab() {
  // #settings, or #installed/3655895377 to land on one wallpaper
  const [want, wid] = (location.hash || "").replace("#", "").split("/");
  const t = want && $(`.tab[data-view="${CSS.escape(want)}"]`);
  if (t) t.click();
  if (wid) openDetail(wid);
}
window.addEventListener("hashchange", openHashTab);
boot().then(() => { openHashTab(); maybeWalkthrough(); });
setInterval(() => { if (App.boot) refreshStatus(); }, 6000);
