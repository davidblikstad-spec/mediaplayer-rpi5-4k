"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

let cfg = { playlists: [], schedules: [], settings: {} };
let mediaList = [];
let currentPlaylistId = null;

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login"; return; }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/* ---------- tabs ---------- */
$$("#tabs button").forEach(b => b.onclick = () => {
  $$("#tabs button").forEach(x => x.classList.remove("active"));
  $$(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "media") loadMedia();
});

/* ---------- load config + media ---------- */
async function loadConfig() {
  cfg = await api("GET", "/api/config");
  renderPlaylistSelect();
  renderSchedules();
  renderSettings();
  renderQuickplay();
}
async function loadMedia() {
  mediaList = await api("GET", "/api/media") || [];
  renderMediaGrid();
  renderDefaultSelect();
}

/* ---------- NOW PLAYING ---------- */
async function pollState() {
  const s = await api("GET", "/api/state");
  if (!s) return;
  const p = s.player || {};
  const fmt = t => t == null ? "–" : (Math.floor(t / 60) + ":" + String(Math.floor(t % 60)).padStart(2, "0"));
  $("#status-box").innerHTML = `
    <div><b>Playing</b> ${p.current ? esc(p.current.file) : (p.playing_default ? "default content" : "nothing")}</div>
    <div><b>Playlist</b> ${p.playlist_name ? esc(p.playlist_name) : "–"} ${p.count ? `(${p.index + 1}/${p.count})` : ""}</div>
    <div><b>Position</b> ${fmt(p.time_pos)} / ${fmt(p.duration)}</div>
    <div><b>Volume</b> ${p.volume != null ? Math.round(p.volume) : "–"}</div>
    <div><b>State</b> ${p.paused ? "paused" : (p.current ? "playing" : "idle")} · player ${s.player_alive ? "ok" : "down"}</div>`;
}
function renderQuickplay() {
  $("#quickplay").innerHTML = "";
  cfg.playlists.forEach(pl => {
    const b = document.createElement("button");
    b.textContent = "▶ " + pl.name;
    b.onclick = () => api("POST", "/api/play/" + pl.id);
    $("#quickplay").appendChild(b);
  });
}
$("#btn-pause").onclick = () => api("POST", "/api/pause");
$("#btn-next").onclick = () => api("POST", "/api/next");
$("#btn-stop").onclick = () => api("POST", "/api/stop");
$$("[data-cec]").forEach(b => b.onclick = async () => {
  const r = await api("POST", "/api/cec", { action: b.dataset.cec });
  $("#cec-out").textContent = (r.ok ? "OK\n" : "FAILED\n") + (r.output || "");
});

/* snapshot refresh */
let snapTimer = null;
function snapshotTick() {
  if (!$("#snap-auto").checked) return;
  if (!$("#tab-now").classList.contains("active")) return;
  const img = $("#snapshot");
  const probe = new Image();
  probe.onload = () => { img.src = probe.src; img.style.display = "block"; $("#snapshot-empty").style.display = "none"; };
  probe.onerror = () => { img.style.display = "none"; $("#snapshot-empty").style.display = "block"; };
  probe.src = "/api/snapshot?t=" + Date.now();
}

/* ---------- PLAYLISTS ---------- */
function renderPlaylistSelect() {
  const sel = $("#pl-select");
  sel.innerHTML = "";
  cfg.playlists.forEach(pl => {
    const o = document.createElement("option");
    o.value = pl.id; o.textContent = pl.name; sel.appendChild(o);
  });
  if (cfg.playlists.length) {
    if (!cfg.playlists.find(p => p.id === currentPlaylistId)) currentPlaylistId = cfg.playlists[0].id;
    sel.value = currentPlaylistId;
  } else currentPlaylistId = null;
  renderPlaylistItems();
}
$("#pl-select").onchange = e => { currentPlaylistId = e.target.value; renderPlaylistItems(); };

function curPlaylist() { return cfg.playlists.find(p => p.id === currentPlaylistId); }

function renderPlaylistItems() {
  const pl = curPlaylist();
  const tb = $("#pl-items tbody");
  tb.innerHTML = "";
  if (!pl) { $("#pl-loop").checked = false; return; }
  $("#pl-loop").checked = !!pl.loop_playlist;
  pl.items.forEach((it, i) => tb.appendChild(itemRow(it, i)));
}

function itemRow(it, i) {
  const tr = document.createElement("tr");
  const isImg = it.type === "image";
  const isStream = it.type === "stream";
  const cell = (html) => { const td = document.createElement("td"); td.innerHTML = html; return td; };
  // order controls
  const td0 = document.createElement("td");
  td0.innerHTML = `<button class="small up">↑</button><button class="small down">↓</button>`;
  tr.appendChild(td0);
  const label = isStream ? (it.name || it.channel || "live") : it.file;
  tr.appendChild(cell(`<span title="${esc(label)}">${esc(label)}</span>`));
  tr.appendChild(cell(isStream ? "live" : it.type));
  // in
  tr.appendChild(cell((isImg || isStream) ? "–" : `<input class="in" type="number" min="0" step="0.1" value="${it.in ?? 0}">`));
  // out / duration (streams: optional seconds to play before advancing; blank = stay live)
  tr.appendChild(cell(isImg
    ? `<input class="dur" type="number" min="1" step="1" value="${it.duration ?? 10}"> s`
    : isStream
      ? `<input class="dur" type="number" min="1" step="1" value="${it.duration ?? ""}" placeholder="live"> s`
      : `<input class="out" type="number" min="0" step="0.1" value="${it.out ?? ""}" placeholder="end">`));
  // loops (streams: subtitles on/off instead)
  tr.appendChild(cell(
    isImg ? "1"
    : isStream ? `<label class="inline"><input type="checkbox" class="subs" ${it.subtitles ? "checked" : ""}> CC</label>`
    : `<input class="loop" value="${it.loop ?? 1}">`));
  // volume
  tr.appendChild(cell(isImg ? "–" : `<input class="vol" type="number" min="0" max="130" value="${it.volume ?? 100}">`));
  // fades
  tr.appendChild(cell(isImg ? "–" : `<input class="fin" type="number" min="0" step="0.1" value="${it.fade_in ?? 0}">`));
  tr.appendChild(cell(isImg ? "–" : `<input class="fout" type="number" min="0" step="0.1" value="${it.fade_out ?? 0}">`));
  // actions
  const tda = document.createElement("td");
  tda.innerHTML = `<button class="small prev">▶</button><button class="small danger del">✕</button>`;
  tr.appendChild(tda);

  td0.querySelector(".up").onclick = () => moveItem(i, -1);
  td0.querySelector(".down").onclick = () => moveItem(i, 1);
  tda.querySelector(".del").onclick = () => { curPlaylist().items.splice(i, 1); renderPlaylistItems(); };
  tda.querySelector(".prev").onclick = () => isStream
    ? toast("Live stream — play the playlist to view it on the TV")
    : previewMedia(it.file, it.type);
  return tr;
}
function moveItem(i, d) {
  const items = curPlaylist().items;
  const j = i + d;
  if (j < 0 || j >= items.length) return;
  [items[i], items[j]] = [items[j], items[i]];
  renderPlaylistItems();
}
function collectItems() {
  const rows = $$("#pl-items tbody tr");
  const pl = curPlaylist();
  return rows.map((tr, i) => {
    const src = pl.items[i];
    const g = (c) => tr.querySelector("." + c);
    if (src.type === "stream") {
      const dv = g("dur").value.trim();
      return {
        id: src.id, type: "stream", provider: src.provider,
        channel: src.channel, name: src.name,
        duration: dv === "" ? null : (parseFloat(dv) || null),
        subtitles: g("subs") ? g("subs").checked : false,
        volume: parseInt(g("vol").value) || 100,
        fade_in: parseFloat(g("fin").value) || 0,
        fade_out: parseFloat(g("fout").value) || 0,
      };
    }
    const o = { id: src.id, file: src.file, type: src.type };
    if (src.type === "image") {
      o.duration = parseFloat(g("dur").value) || 10;
      o.loop = 1;
    } else {
      o.in = parseFloat(g("in").value) || 0;
      o.out = g("out").value === "" ? null : parseFloat(g("out").value);
      const lv = g("loop").value.trim();
      o.loop = (lv === "" || lv === "0") ? "always" : (isNaN(+lv) ? lv : +lv);
      o.volume = parseInt(g("vol").value) || 100;
      o.fade_in = parseFloat(g("fin").value) || 0;
      o.fade_out = parseFloat(g("fout").value) || 0;
    }
    return o;
  });
}
async function persistPlaylist(pl) {
  await api("PUT", "/api/playlists/" + pl.id, { items: pl.items, loop_playlist: pl.loop_playlist });
}
$("#pl-save").onclick = async () => {
  const pl = curPlaylist(); if (!pl) return;
  pl.items = collectItems();
  pl.loop_playlist = $("#pl-loop").checked;
  await persistPlaylist(pl);
  toast("Playlist saved");
};
async function createPlaylist() {
  const name = prompt("Playlist name", "New playlist"); if (!name) return null;
  const pl = await api("POST", "/api/playlists", { name });
  await loadConfig(); currentPlaylistId = pl.id; renderPlaylistSelect();
  return curPlaylist();
}
$("#pl-new").onclick = createPlaylist;
$("#pl-rename").onclick = async () => {
  const pl = curPlaylist(); if (!pl) return;
  const name = prompt("Rename playlist", pl.name); if (!name) return;
  await api("PUT", "/api/playlists/" + pl.id, { name });
  await loadConfig();
};
$("#pl-delete").onclick = async () => {
  const pl = curPlaylist(); if (!pl) return;
  if (!confirm("Delete playlist '" + pl.name + "'?")) return;
  await api("DELETE", "/api/playlists/" + pl.id);
  currentPlaylistId = null; await loadConfig();
};
$("#pl-play").onclick = async () => { const pl = curPlaylist(); if (pl) { await api("POST", "/api/play/" + pl.id); toast("Playing " + pl.name); } };
$("#pl-add").onclick = async () => {
  let pl = curPlaylist();
  if (!pl) { pl = await createPlaylist(); if (!pl) return; }
  openPicker(async (m) => {
    const cur = curPlaylist(); if (!cur) return;
    cur.items = collectItems();              // keep any unsaved edits in existing rows
    cur.loop_playlist = $("#pl-loop").checked;
    const it = { id: rid(), file: m.file, type: m.type };
    if (m.type === "image") { it.duration = 10; it.loop = 1; }
    else { it.in = 0; it.out = null; it.loop = 1; it.volume = 100; it.fade_in = 0; it.fade_out = 0; }
    cur.items.push(it); renderPlaylistItems();
    await persistPlaylist(cur);
    toast("Added & saved " + it.file + " — pick more, or close (×).");
  });
};
$("#pl-add-stream").onclick = async () => {
  let pl = curPlaylist();
  if (!pl) { pl = await createPlaylist(); if (!pl) return; }
  const channels = await api("GET", "/api/streams") || [];
  const wrap = document.createElement("div");
  wrap.innerHTML = `<h3>Add a live channel</h3><div class="chips"></div>`;
  const chips = wrap.querySelector(".chips");
  channels.forEach(ch => {
    const b = document.createElement("button");
    b.className = "primary"; b.textContent = ch.name;
    b.onclick = async () => {
      const cur = curPlaylist(); if (!cur) return;
      cur.items = collectItems();
      cur.loop_playlist = $("#pl-loop").checked;
      cur.items.push({ id: rid(), type: "stream", provider: "nrk", channel: ch.id,
        name: ch.name, duration: null, subtitles: false, volume: 100, fade_in: 0, fade_out: 0 });
      renderPlaylistItems();
      await persistPlaylist(cur);
      $("#modal-close").onclick();
      toast("Added " + ch.name + " (live)");
    };
    chips.appendChild(b);
  });
  openModal(wrap);
};

/* ---------- SCHEDULES ---------- */
function renderSchedules() {
  const wrap = $("#sch-list"); wrap.innerHTML = "";
  if (!cfg.schedules.length) wrap.innerHTML = '<p class="muted">No schedules yet.</p>';
  cfg.schedules.forEach(s => wrap.appendChild(schedCard(s)));
}
function schedCard(s) {
  const card = document.createElement("div"); card.className = "card";
  const plOpts = cfg.playlists.map(p => `<option value="${p.id}" ${p.id === s.playlist_id ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  card.innerHTML = `
    <div class="sch">
      <label class="inline"><input type="checkbox" class="en" ${s.enabled ? "checked" : ""}> enabled</label>
      <input class="nm" value="${esc(s.name)}">
      <select class="kind">
        <option value="play_playlist" ${s.kind === "play_playlist" ? "selected" : ""}>Play playlist</option>
        <option value="stop" ${s.kind === "stop" ? "selected" : ""}>Stop (default)</option>
        <option value="cec" ${s.kind === "cec" ? "selected" : ""}>Display (CEC)</option>
      </select>
      <select class="pl" ${s.kind === "play_playlist" ? "" : "style=display:none"}>${plOpts}</select>
      <select class="cec" ${s.kind === "cec" ? "" : "style=display:none"}>
        <option value="on" ${s.cec_action === "on" ? "selected" : ""}>On</option>
        <option value="off" ${s.cec_action === "off" ? "selected" : ""}>Off</option>
        <option value="source" ${s.cec_action === "source" ? "selected" : ""}>Set source</option>
      </select>
      <label class="inline">at <input class="tm" type="time" value="${s.time}"></label>
      <span class="spacer"></span>
      <button class="save primary small">Save</button>
      <button class="del danger small">Delete</button>
    </div>
    <div class="days">${DAYS.map((d, i) => `<label>${d}<input type="checkbox" class="day" data-d="${i}" ${(s.days || []).includes(i) ? "checked" : ""}></label>`).join("")}</div>
    <div class="muted next"></div>`;
  const kindSel = card.querySelector(".kind");
  kindSel.onchange = () => {
    card.querySelector(".pl").style.display = kindSel.value === "play_playlist" ? "" : "none";
    card.querySelector(".cec").style.display = kindSel.value === "cec" ? "" : "none";
  };
  card.querySelector(".save").onclick = async () => {
    const body = {
      enabled: card.querySelector(".en").checked,
      name: card.querySelector(".nm").value,
      kind: kindSel.value,
      playlist_id: card.querySelector(".pl").value,
      cec_action: card.querySelector(".cec").value,
      time: card.querySelector(".tm").value,
      days: $$(".day", card).filter(c => c.checked).map(c => +c.dataset.d),
    };
    await api("PUT", "/api/schedules/" + s.id, body);
    await loadConfig(); toast("Schedule saved");
  };
  card.querySelector(".del").onclick = async () => {
    if (!confirm("Delete schedule?")) return;
    await api("DELETE", "/api/schedules/" + s.id); await loadConfig();
  };
  return card;
}
$("#sch-new").onclick = async () => {
  await api("POST", "/api/schedules", { name: "New schedule", kind: "play_playlist", time: "08:00", days: [0, 1, 2, 3, 4] });
  await loadConfig();
};

/* ---------- MEDIA ---------- */
// On the Pi 5 every file plays — these only describe *how*, and which
// transcode (if any) is worth offering.
const PLAY_MODE = {
  hw:    { cls: "ok",   text: "HEVC · hardware 4K" },
  sw:    { cls: "ok",   text: "software ≤1080p" },
  heavy: { cls: "warn", text: "software >1080p — may stutter" },
};
function renderMediaGrid() {
  const g = $("#media-grid"); g.innerHTML = "";
  if (!mediaList.length) { g.innerHTML = '<p class="muted">No media uploaded yet.</p>'; return; }
  mediaList.forEach(m => {
    const job = transcodeJobs[m.file];
    const transcoding = job && job.status === "running";
    const mode = m.play_mode;            // "hw" | "sw" | "heavy" | "ok"(non-video)
    const c = document.createElement("div");
    c.className = "media-card" + (mode === "heavy" ? " heavy" : "");
    const thumb = m.thumb ? `<img src="/thumb/${encodeURIComponent(m.thumb)}">` : `<span class="muted">${m.type}</span>`;
    const dur = m.duration ? " · " + Math.round(m.duration) + "s" : "";
    const res = (m.width && m.height) ? " · " + m.width + "×" + m.height : "";
    const cod = m.codec ? " · " + m.codec : "";
    let badge = "";
    if (transcoding) {
      const tgt = job.target === "1080p" ? "1080p" : "HEVC";
      badge = `<div class="badge warn">Transcoding → ${tgt} ${job.percent || 0}%</div>`;
    } else if (PLAY_MODE[mode]) {
      badge = `<div class="badge ${PLAY_MODE[mode].cls}">${PLAY_MODE[mode].text}</div>`;
    }
    let actions = `<button class="small prev">Preview</button>`;
    if (transcoding) {
      actions += `<button class="small danger abort">Abort</button>`;
    } else if (m.type === "video") {
      // Transcode is always optional. Offer "→ HEVC" (gain hardware decode,
      // keep resolution) for anything not already HEVC, and "→ 1080p" to shrink.
      if (mode !== "hw") actions += `<button class="small primary tc" data-target="hevc">→ HEVC</button>`;
      actions += `<button class="small tc" data-target="1080p">→ 1080p</button>`;
    }
    actions += `<button class="small danger del">Delete</button>`;
    c.innerHTML = `
      <div class="thumb">${thumb}${badge}</div>
      <div class="meta"><div class="name">${esc(m.file)}</div><div class="muted">${m.type}${cod}${res}${dur} · ${(m.size / 1048576).toFixed(1)} MB</div></div>
      <div class="actions">${actions}</div>`;
    c.querySelector(".thumb").onclick = () => previewMedia(m.file, m.type);
    c.querySelector(".prev").onclick = () => previewMedia(m.file, m.type);
    c.querySelector(".del").onclick = async () => {
      if (!confirm("Delete " + m.file + "?")) return;
      await api("DELETE", "/api/media/" + encodeURIComponent(m.file)); loadMedia();
    };
    $$(".tc", c).forEach(tc => tc.onclick = async () => {
      const target = tc.dataset.target;
      await api("POST", "/api/transcode/start", { file: m.file, target });
      toast(`Transcoding ${m.file} → ${target}…`); ensureTranscodePolling(); loadMedia();
    });
    const ab = c.querySelector(".abort");
    if (ab) ab.onclick = () => abortTranscode(m.file);
    g.appendChild(c);
  });
}
async function abortTranscode(file) {
  if (!confirm("Abort transcoding " + file + "?")) return;
  await api("DELETE", "/api/transcode/" + encodeURIComponent(file));
  toast("Aborting " + file + "…");
}
let uploadXhr = null;        // in-flight upload, so Cancel can abort it
let uploadCancelled = false;
function setUploadBar(frac) { const b = $("#upload-bar"); if (b) b.style.width = Math.round(frac * 100) + "%"; }
function showUploadProgress(on) { const p = $("#upload-progress"); if (p) p.classList.toggle("hidden", !on); }
// XHR (not fetch) so we get upload.onprogress and .abort()
function uploadOne(f, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    uploadXhr = xhr;
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      uploadXhr = null;
      if (xhr.status === 401) { location.href = "/login"; return; }
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.error || ("HTTP " + xhr.status)));
    };
    xhr.onerror = () => { uploadXhr = null; reject(new Error("network/size")); };
    xhr.onabort = () => { uploadXhr = null; reject(new DOMException("aborted", "AbortError")); };
    const fd = new FormData(); fd.append("file", f);
    xhr.send(fd);
  });
}
async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  uploadCancelled = false;
  showUploadProgress(true);
  let done = 0, transcoding = false;
  for (const f of files) {
    if (uploadCancelled) break;
    $("#upload-status").textContent = `Uploading ${f.name}… (${done + 1}/${files.length})`;
    setUploadBar(0);
    try {
      const data = await uploadOne(f, setUploadBar);
      if (data.transcode && data.transcode.needed) transcoding = true;
    } catch (e) {
      showUploadProgress(false);
      $("#upload-status").textContent = e.name === "AbortError"
        ? `Upload cancelled${done ? ` — ${done} added` : ""}.`
        : `Upload failed for ${f.name}: ${e.message}`;
      loadMedia();
      return;
    }
    done++;
    loadMedia();
  }
  showUploadProgress(false);
  if (transcoding) {
    $("#upload-status").textContent =
      `Added ${done} file${done > 1 ? "s" : ""}. Auto-transcoding per your policy (software encode — can take a while on the Pi).`;
    ensureTranscodePolling();
  } else {
    $("#upload-status").textContent = `Added ${done} file${done > 1 ? "s" : ""}.`;
  }
}
$("#upload-btn").onclick = () => $("#upload-input").click();
$("#upload-input").onchange = async (e) => {
  await uploadFiles(e.target.files);
  e.target.value = "";   // allow re-selecting the same file
};
{ const uc = $("#upload-cancel"); if (uc) uc.onclick = () => { uploadCancelled = true; if (uploadXhr) uploadXhr.abort(); }; }
(() => {
  const dz = $("#dropzone");
  if (!dz) return;
  const stop = e => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter", "dragover"].forEach(ev =>
    dz.addEventListener(ev, e => { stop(e); dz.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev =>
    dz.addEventListener(ev, e => { stop(e); dz.classList.remove("dragover"); }));
  dz.addEventListener("drop", e => uploadFiles(e.dataTransfer.files));
})();

/* ---------- transcode progress ---------- */
let transcodePollTimer = null;
let transcodeJobs = {};      // latest /api/transcode snapshot (for card badges)
const transcodeSeen = {};   // file -> true once its done/error was reported
function ensureTranscodePolling() { if (!transcodePollTimer) pollTranscodes(); }
async function pollTranscodes() {
  transcodePollTimer = null;
  let jobs = await api("GET", "/api/transcode") || {};
  transcodeJobs = jobs;
  renderTranscodes(jobs);
  let active = false;
  Object.keys(jobs).forEach(k => {
    const j = jobs[k];
    if (j.status === "running") active = true;
    if (["done", "error", "aborted"].includes(j.status) && !transcodeSeen[k]) {
      transcodeSeen[k] = true;
      if (j.status === "done") toast(`Transcoded → ${j.target === "1080p" ? "1080p" : "HEVC"}: ` + (j.result || j.file));
      else if (j.status === "aborted") toast("Transcode aborted: " + j.file);
      else toast("Transcode failed: " + j.file);
      loadMedia();
    }
  });
  if (active) { renderMediaGrid(); transcodePollTimer = setTimeout(pollTranscodes, 2000); }
}
function renderTranscodes(jobs) {
  const wrap = $("#transcode-list"); if (!wrap) return;
  wrap.innerHTML = "";
  // show only in-progress jobs and errors; completed ones drop off (file appears in grid)
  Object.keys(jobs).forEach(k => {
    const j = jobs[k];
    if (j.status !== "running" && j.status !== "error") return;
    const pct = j.status === "running" ? (j.percent || 0) : 0;
    const right = j.status === "running" ? pct + "%" : "failed";
    const div = document.createElement("div");
    div.className = "transcode-job" + (j.status === "error" ? " error" : "");
    div.innerHTML =
      `<div class="label"><span>Transcoding <b>${esc(j.file)}</b> (${esc(j.from || "")} → ${j.target === "1080p" ? "1080p" : "HEVC"})</span><span>${esc(right)}</span></div>` +
      (j.status === "error"
        ? `<div class="muted">${esc(j.error || "ffmpeg error")}</div>`
        : `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`);
    if (j.status === "running") {
      const ab = document.createElement("button");
      ab.className = "small danger"; ab.textContent = "Abort";
      ab.style.marginTop = ".4rem";
      ab.onclick = () => abortTranscode(j.file);
      div.appendChild(ab);
    }
    wrap.appendChild(div);
  });
}

/* ---------- SETTINGS ---------- */
function renderSettings() {
  const s = cfg.settings || {};
  $("#set-cec-dev").value = s.cec_device || "/dev/cec0";
  $("#set-cec-phys").value = s.cec_phys_addr || "";
  $("#set-snap").value = s.screenshot_interval || 5;
  $("#set-avdelay").value = s.stream_av_delay_ms ?? 0;
  $("#set-resync").value = Math.round((s.stream_resync_interval_s ?? 3600) / 60);
  $("#set-ao").value = (s.audio_out || "auto").replace(/^alsa\//, "");
  $("#set-tc-policy").value = s.transcode_policy || "off";
  $("#pw-user").value = (cfg.auth && cfg.auth.username) || "";
  if (s.default_item) $("#set-default-dur").value = s.default_item.duration || 10;
}
function renderDefaultSelect() {
  const sel = $("#set-default"); sel.innerHTML = '<option value="">— none (black screen) —</option>';
  mediaList.forEach(m => {
    const o = document.createElement("option"); o.value = m.file; o.textContent = m.file + " (" + m.type + ")";
    sel.appendChild(o);
  });
  if (cfg.settings.default_item) sel.value = cfg.settings.default_item.file;
}
$("#set-save").onclick = async () => {
  const file = $("#set-default").value;
  let def = null;
  if (file) {
    const m = mediaList.find(x => x.file === file) || { type: "video" };
    def = { file, type: m.type, loop: "always", volume: 100, fade_in: 0, fade_out: 0 };
    if (m.type === "image") def.duration = parseInt($("#set-default-dur").value) || 10;
  }
  cfg = await api("PUT", "/api/settings", {
    cec_device: $("#set-cec-dev").value,
    cec_phys_addr: $("#set-cec-phys").value,
    screenshot_interval: parseInt($("#set-snap").value) || 5,
    stream_av_delay_ms: parseInt($("#set-avdelay").value) || 0,
    stream_resync_interval_s: (parseInt($("#set-resync").value) || 0) * 60,
    audio_out: $("#set-ao").value,
    transcode_policy: $("#set-tc-policy").value,
    default_item: def,
  });
  toast("Settings saved");
};
$("#cec-detect").onclick = async () => {
  $("#cec-info").textContent = "Scanning…";
  const r = await api("GET", "/api/cec/info");
  $("#cec-info").textContent = (r.phys_addr ? "Physical address: " + r.phys_addr + "\n\n" : "") + (r.output || "");
  if (r.phys_addr && !$("#set-cec-phys").value) $("#set-cec-phys").value = r.phys_addr;
};
$("#pw-save").onclick = async () => {
  const r = await api("PUT", "/api/password", {
    username: $("#pw-user").value, old: $("#pw-old").value, new: $("#pw-new").value,
  });
  $("#pw-status").textContent = r.ok ? "Updated" : (r.error || "failed");
  $("#pw-old").value = ""; $("#pw-new").value = "";
};

/* ---------- modal / picker / preview ---------- */
function openModal(html) { $("#modal-content").innerHTML = ""; if (typeof html === "string") $("#modal-content").innerHTML = html; else $("#modal-content").appendChild(html); $("#modal").classList.remove("hidden"); }
$("#modal-close").onclick = () => { $("#modal").classList.add("hidden"); $("#modal-content").innerHTML = ""; };
$("#modal").onclick = e => { if (e.target.id === "modal") $("#modal-close").onclick(); };

function previewMedia(file, type) {
  const url = "/media/" + file.split("/").map(encodeURIComponent).join("/");
  if (type === "image") openModal(`<h3>${esc(file)}</h3><img src="${url}">`);
  else if (type === "video") openModal(`<h3>${esc(file)}</h3><video src="${url}" controls autoplay style="max-width:80vw"></video><p class="muted">In-browser preview depends on browser codec support (mp4/webm work best).</p>`);
  else openModal(`<h3>${esc(file)}</h3><audio src="${url}" controls autoplay></audio>`);
}
async function openPicker(onPick) {
  if (!mediaList.length) await loadMedia();
  const wrap = document.createElement("div");
  wrap.innerHTML = `<h3>Choose a file</h3><div class="picker-list"></div>`;
  const list = wrap.querySelector(".picker-list");
  mediaList.forEach(m => {
    const c = document.createElement("div");
    c.className = "media-card" + (m.play_mode === "heavy" ? " heavy" : "");
    const badge = m.play_mode === "heavy" ? `<div class="badge warn">software >1080p</div>` : "";
    c.innerHTML = `<div class="thumb">${m.thumb ? `<img src="/thumb/${encodeURIComponent(m.thumb)}">` : `<span class="muted">${m.type}</span>`}${badge}</div><div class="meta"><div class="name">${esc(m.file)}</div></div>`;
    c.onclick = () => onPick(m);
    list.appendChild(c);
  });
  openModal(wrap);
}

/* ---------- utils ---------- */
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function rid() { return Math.random().toString(36).slice(2, 14); }
let toastTimer;
function toast(msg) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.style.cssText = "position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);background:#23272f;border:1px solid #2e333d;padding:.5rem 1rem;border-radius:8px;z-index:30"; document.body.appendChild(t); }
  t.textContent = msg; t.style.display = "block";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.style.display = "none", 2000);
}

/* ---------- boot ---------- */
loadConfig();
loadMedia();
ensureTranscodePolling();   // resume showing any transcode already in progress
pollState();
setInterval(pollState, 2000);
setInterval(snapshotTick, 4000);
snapshotTick();
