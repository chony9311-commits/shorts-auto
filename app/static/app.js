/* shorts-factory UI
 * 미리보기 캔버스는 서버(FFmpeg)와 똑같은 크롭/줌/팬 계산을 그대로 수행한다.
 * 즉 "보이는 그대로" 렌더된다. 계산식을 바꿀 때는 app/render.py의 compute_crop과
 * 반드시 함께 고쳐야 한다.
 */
const OUT = { w: 1080, h: 1920 };
const LS_KEY = "shorts-factory:v1";
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const canvas = $("#stage");
const ctx = canvas.getContext("2d");
const CW = canvas.width;
const CH = canvas.height;
const S = CW / OUT.w; // 1080 -> 540

const state = {
  cfg: null,
  mode: "shopping",
  clips: [],
  active: 0,
  title: {
    preset: "chomolip",
    lines: ["", ""],
    badge_text: "",
    badge_number_text: "",
    credit: "",
    font_scale: 1.0,
  },
  thumb: {
    dim: 0,
    source: { type: "frame", clip_id: null, time: null, image_id: null, as_is: false },
    prepend: false,
    prependDur: 1.0,
    imageName: null,
  },
  audio: { source: "clip", audio_id: null, volume: 1.0, items: [] },
  sub: { enabled: true, id: null },
  maskMode: "blur",
  jobId: null,
  playing: false,
};

const video = $("#hiddenVideo");
video.muted = true;

/* ------------------------------------------------------------------ util */
function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), bad ? 6000 : 2800);
}

function saveLocal() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      mode: state.mode, title: state.title,
      thumb: { dim: state.thumb.dim, prepend: state.thumb.prepend, prependDur: state.thumb.prependDur },
      audio: { source: state.audio.source, audio_id: state.audio.audio_id, volume: state.audio.volume },
      sub: state.sub,
    }));
  } catch (e) { /* ignore */ }
}

function loadLocal() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const d = JSON.parse(raw);
    if (d.title) Object.assign(state.title, d.title);
    if (d.thumb) Object.assign(state.thumb, d.thumb);
    if (d.audio) Object.assign(state.audio, d.audio);
    if (d.sub) Object.assign(state.sub, d.sub);
    if (d.mode) state.mode = d.mode;
  } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ 서버 통신 */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (body && body.detail) || body || `요청 실패 (${res.status})`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body;
}

/* ------------------------------------------------------------------ 초기화 */
async function init() {
  loadLocal();
  try {
    state.cfg = await api("/api/state");
  } catch (e) {
    toast("서버 상태를 읽지 못했습니다: " + e.message, true);
    return;
  }
  const h = $("#health");
  if (state.cfg.ffmpeg) {
    h.className = "chip chip-ok";
    h.textContent = "ffmpeg OK";
  } else {
    h.className = "chip chip-bad";
    h.textContent = "ffmpeg 없음";
    toast(state.cfg.ffmpeg_message + "\n\n터미널: brew install ffmpeg", true);
  }
  if (!state.cfg.font_ok) {
    toast("한글 폰트를 찾지 못했습니다. fonts/ 폴더에 NanumGothicBold.ttf 같은 폰트를 넣어주세요.", true);
  }

  renderPresets();
  fillVoices();
  bindEvents();
  applyMode(state.mode);
  syncTitleInputs();
  await refreshClips();
  draw();
  saveLocal();
}

function renderPresets() {
  const grid = $("#presetGrid");
  grid.innerHTML = "";
  state.cfg.presets.forEach((p) => {
    const el = document.createElement("button");
    el.className = "preset" + (p.key === state.title.preset ? " active" : "");
    el.dataset.key = p.key;
    const bars = (p.bars || []).slice(0, 2);
    const pv = bars.map((b, i) => `<span style="background:${b.bg};color:${b.color}">${
      i === 0 ? "제목 첫 줄" : "둘째 줄"}</span>`).join("");
    el.innerHTML = `<div class="pv">${pv || '<span style="background:#333;color:#fff">제목</span>'}</div>
      <div class="nm">${p.label}</div><div class="ds">${p.desc}</div>`;
    el.addEventListener("click", () => {
      state.title.preset = p.key;
      $$(".preset").forEach((x) => x.classList.toggle("active", x.dataset.key === p.key));
      saveLocal();
      draw();
    });
    grid.appendChild(el);
  });
}

function fillVoices() {
  const sel = $("#ttsVoice");
  sel.innerHTML = "";
  (state.cfg.voices || []).forEach((v) => {
    const o = document.createElement("option");
    o.value = v.id;
    o.textContent = v.label;
    sel.appendChild(o);
  });
}

/* ------------------------------------------------------------------ 모드 */
function applyMode(mode) {
  state.mode = mode;
  $$("#modeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#badgeNumHint").textContent = mode === "shopping" ? "(비우면 1/3 자동)" : "(셀럽 모드에선 번호 배지 없음)";
  draw();
}

/* ------------------------------------------------------------------ 클립 */
async function refreshClips() {
  try {
    const data = await api("/api/clips");
    const byId = new Map(state.clips.map((c) => [c.id, c]));
    state.clips = (data.clips || []).map((m) => {
      const prev = byId.get(m.id);
      return prev || {
        id: m.id, name: m.name, duration: m.duration, width: m.width, height: m.height,
        has_audio: m.has_audio, start: 0, end: m.duration, zoom: 1, pan_x: 0, pan_y: 0,
        mute: false, masks: [],
      };
    });
    if (state.active >= state.clips.length) state.active = Math.max(0, state.clips.length - 1);
    renderClipList();
    setActive(state.active, true);
    updateTotals();
  } catch (e) {
    toast("클립 목록 실패: " + e.message, true);
  }
}

function renderClipList() {
  const list = $("#clipList");
  list.innerHTML = "";
  $("#clipHint").hidden = state.clips.length > 0;
  state.clips.forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "clip" + (i === state.active ? " active" : "");
    el.innerHTML = `
      <div class="no">${i + 1}</div>
      <video class="thumb" muted preload="metadata" src="/api/clips/${c.id}/file#t=0.1"></video>
      <div class="meta">
        <div class="name" title="${c.name}">${c.name}</div>
        <div class="sub">${fmt(c.duration)} · ${c.width}×${c.height}${c.has_audio ? "" : " · 무음"}${
          state.mode === "shopping" ? ` · 배지 ${i + 1}/${state.clips.length}` : ""}</div>
        <div class="trim">
          <input type="number" step="0.1" min="0" max="${c.duration}" value="${(+c.start).toFixed(1)}" data-k="start" title="시작(초)" />
          <input type="number" step="0.1" min="0" max="${c.duration}" value="${(+c.end).toFixed(1)}" data-k="end" title="끝(초)" />
        </div>
      </div>
      <div class="tools">
        <button class="icon-btn" data-act="up" title="위로">↑</button>
        <button class="icon-btn" data-act="mute" title="소리 끄기">${c.mute ? "🔇" : "🔊"}</button>
        <button class="icon-btn" data-act="down" title="아래로">↓</button>
        <button class="icon-btn del" data-act="del" title="삭제">✕</button>
      </div>`;
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("input") || ev.target.closest("button")) return;
      setActive(i);
    });
    el.querySelectorAll(".trim input").forEach((inp) => {
      inp.addEventListener("change", () => {
        const k = inp.dataset.k;
        let v = parseFloat(inp.value);
        if (isNaN(v)) v = k === "start" ? 0 : c.duration;
        v = Math.max(0, Math.min(c.duration, v));
        if (k === "start") c.start = Math.min(v, c.end - 0.2);
        else c.end = Math.max(v, c.start + 0.2);
        renderClipList();
        updateTotals();
        if (i === state.active) { setActive(i, true); }
      });
    });
    el.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const act = btn.dataset.act;
        if (act === "del") {
          state.clips.splice(i, 1);
          if (state.active >= state.clips.length) state.active = Math.max(0, state.clips.length - 1);
          renderClipList(); setActive(state.active, true); updateTotals();
        } else if (act === "mute") {
          c.mute = !c.mute; renderClipList();
        } else if (act === "up" && i > 0) {
          [state.clips[i - 1], state.clips[i]] = [state.clips[i], state.clips[i - 1]];
          state.active = i - 1; renderClipList(); setActive(state.active, true);
        } else if (act === "down" && i < state.clips.length - 1) {
          [state.clips[i + 1], state.clips[i]] = [state.clips[i], state.clips[i + 1]];
          state.active = i + 1; renderClipList(); setActive(state.active, true);
        }
      });
    });
    list.appendChild(el);
  });
  renderMaskList();
}

function setActive(i, reload) {
  state.active = i;
  const c = state.clips[i];
  if (!c) { draw(); return; }
  if (reload || video.dataset.clip !== c.id) {
    video.dataset.clip = c.id;
    video.src = `/api/clips/${c.id}/file`;
    video.addEventListener("loadedmetadata", () => {
      try { video.currentTime = c.start + 0.02; } catch (e) { /* ignore */ }
      draw();
    }, { once: true });
  }
  const z = $("#zoom");
  z.value = Math.round((c.zoom || 1) * 100);
  $("#panX").value = Math.round((c.pan_x || 0) * 100);
  $("#panY").value = Math.round((c.pan_y || 0) * 100);
  $("#zoomVal").textContent = z.value + "%";
  $("#panXVal").textContent = $("#panX").value;
  $("#panYVal").textContent = $("#panY").value;
  renderMaskList();
  draw();
}

function fmt(sec) {
  sec = Math.max(0, +sec || 0);
  return sec.toFixed(1) + "초";
}

function updateTotals() {
  const sum = state.clips.reduce((a, c) => a + Math.max(0, c.end - c.start), 0)
    + (state.thumb.prepend ? state.thumb.prependDur : 0);
  $("#totalDur").textContent = "총 " + sum.toFixed(1) + "초";
  $("#segCount").textContent = state.clips.length + "컷";
  const b = $("#durBadge");
  if (!state.clips.length) { b.className = "chip"; b.textContent = "20~24초 권장"; return; }
  if (sum < 20) { b.className = "chip chip-warn"; b.textContent = "조금 짧아요 (20~24초 권장)"; }
  else if (sum > 24) { b.className = "chip chip-warn"; b.textContent = "조금 길어요 (20~24초 권장)"; }
  else { b.className = "chip chip-ok"; b.textContent = "리듬 좋아요"; }
}

/* ------------------------------------------------------------------ 미리보기 그리기 */
function activeClip() { return state.clips[state.active]; }

function computeCrop(sw, sh, zoom, panX, panY) {
  if (!sw || !sh) return null;
  zoom = Math.min(2, Math.max(1, +zoom || 1));
  const targetAR = OUT.w / OUT.h;
  let baseW, baseH;
  if (sw / sh >= targetAR) { baseH = sh; baseW = sh * targetAR; }
  else { baseW = sw; baseH = sw / targetAR; }
  const cw = Math.min(baseW / zoom, sw);
  const ch = Math.min(baseH / zoom, sh);
  const slackX = Math.max(0, sw - cw);
  const slackY = Math.max(0, sh - ch);
  const x = slackX * (0.5 + Math.min(1, Math.max(-1, panX || 0)) / 2);
  const y = slackY * (0.5 + Math.min(1, Math.max(-1, panY || 0)) / 2);
  return { x, y, w: cw, h: ch };
}

function parseHighlight(line) {
  const parts = String(line || "").split(/(\*\*.+?\*\*)/).filter((s) => s !== "");
  return parts.map((p) => {
    if (p.startsWith("**") && p.endsWith("**") && p.length > 4) return { text: p.slice(2, -2), hl: true };
    return { text: p.replace(/\*\*/g, ""), hl: false };
  });
}

function draw() {
  const c = activeClip();
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, CW, CH);
  $("#stageHint").hidden = !!c;
  if (!c) return;

  const crop = computeCrop(video.videoWidth, video.videoHeight, c.zoom, c.pan_x, c.pan_y);
  if (crop && video.readyState >= 2) {
    ctx.drawImage(video, crop.x, crop.y, crop.w, crop.h, 0, 0, CW, CH);
  }

  const dim = state.thumb.dim;
  if (dim > 0) { ctx.fillStyle = `rgba(0,0,0,${dim})`; ctx.fillRect(0, 0, CW, CH); }

  const barH = drawDecor();
  drawMasks(c, barH);
}

function drawDecor() {
  const t = state.title;
  const p = (state.cfg.presets || []).find((x) => x.key === t.preset) || (state.cfg.presets || [])[0];
  if (!p) return 0;
  const lines = [t.lines[0], t.lines[1]].map((x) => (x || "").trim()).filter(Boolean);
  const margin = p.margin_x * S;
  const padY = p.pad_y * S;
  const gap = p.line_gap * S;
  let font = p.font_size * (t.font_scale || 1) * S;

  const usable = CW - margin * 2;
  ctx.font = `900 ${font}px "Apple SD Gothic Neo","Pretendard","Malgun Gothic",sans-serif`;
  let widest = 1;
  lines.forEach((l) => {
    const w = parseHighlight(l).reduce((a, seg) => a + ctx.measureText(seg.text).width, 0);
    widest = Math.max(widest, w);
  });
  if (widest > usable) {
    font = Math.max(11, font * usable / widest);
    ctx.font = `900 ${font}px "Apple SD Gothic Neo","Pretendard","Malgun Gothic",sans-serif`;
  }
  const lineH = font * 1.18;
  let y = 0;

  lines.forEach((line, idx) => {
    const style = p.bars[Math.min(idx, p.bars.length - 1)] || { bg: "#111", color: "#fff" };
    ctx.fillStyle = style.bg;
    ctx.fillRect(0, y, CW, lineH);
    const segs = parseHighlight(line);
    const totalW = segs.reduce((a, s2) => a + ctx.measureText(s2.text).width, 0);
    let x = p.align === "left" ? margin : Math.max(margin, (CW - totalW) / 2);
    if (p.highlight) {
      let cx = x;
      segs.forEach((s2) => {
        const w2 = ctx.measureText(s2.text).width;
        if (s2.hl) {
          ctx.fillStyle = p.highlight;
          ctx.fillRect(cx - font * 0.06, y + lineH * 0.14, w2 + font * 0.12, lineH * 0.72);
        }
        cx += w2;
      });
    }
    ctx.fillStyle = style.color;
    ctx.textBaseline = "top";
    ctx.fillText(segs.map((s2) => s2.text).join(""), x, y + lineH * 0.1);
    y += lineH + gap;
  });

  const barH = y;
  if (!lines.length) return 0;

  // 배지
  const bFont = 58 * (t.font_scale || 1) * S;
  const bh = bFont * 1.62;
  const by = barH + 20 * S;
  ctx.font = `900 ${bFont}px "Apple SD Gothic Neo","Pretendard","Malgun Gothic",sans-serif`;
  ctx.textBaseline = "top";
  const drawChip = (text, right) => {
    if (!text) return;
    const tw = ctx.measureText(text).width;
    const bw = tw + bFont * 0.9;
    const bx = right ? CW - margin - bw : margin;
    ctx.fillStyle = p.badgeBg;
    ctx.fillRect(bx, by, bw, bh);
    ctx.fillStyle = p.badgeColor;
    ctx.fillText(text, bx + bFont * 0.45, by + bh * 0.22);
  };
  drawChip(t.badge_text, false);
  if (state.mode === "shopping") {
    const num = t.badge_number_text || `${state.active + 1}/${Math.max(1, state.clips.length)}`;
    drawChip(num, true);
  }

  // 출처 태그
  if (t.credit) {
    const f2 = 40 * (t.font_scale || 1) * S;
    ctx.font = `700 ${f2}px "Apple SD Gothic Neo","Pretendard",sans-serif`;
    const tw = ctx.measureText(t.credit).width;
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(CW - tw - 64 * S, CH - f2 * 1.7 - 90 * S, tw + 48 * S, f2 * 1.7);
    ctx.fillStyle = "#fff";
    ctx.fillText(t.credit, CW - tw - 40 * S, CH - f2 * 1.7 - 90 * S + f2 * 0.35);
  }
  return barH;
}

function drawMasks(c, barH) {
  const masks = c.masks || [];
  masks.forEach((m, j) => {
    const x = m.x * CW, y = m.y * CH, w = m.w * CW, h = m.h * CH;
    if (m.mode === "box" && m.text) {
      ctx.fillStyle = "#000";
      ctx.fillRect(x, y, w, h);
      ctx.fillStyle = "#fff";
      ctx.font = `700 ${Math.max(9, h * 0.55)}px "Apple SD Gothic Neo",sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(m.text, x + w / 2, y + h / 2);
      ctx.textAlign = "left";
    } else if (m.mode === "blur") {
      try {
        const sx = Math.max(0, Math.round(x)), sy = Math.max(0, Math.round(y));
        const sw = Math.max(1, Math.round(w)), sh = Math.max(1, Math.round(h));
        const tmp = document.createElement("canvas");
        tmp.width = Math.max(1, sw / 8); tmp.height = Math.max(1, sh / 8);
        tmp.getContext("2d").drawImage(canvas, sx, sy, sw, sh, 0, 0, tmp.width, tmp.height);
        ctx.save();
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(tmp, sx, sy, sw, sh);
        ctx.restore();
      } catch (e) { /* ignore */ }
    } else {
      ctx.fillStyle = "rgba(0,0,0,0.55)";
      ctx.fillRect(x, y, w, h);
    }
    ctx.strokeStyle = "#ffd400";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
    ctx.fillStyle = "#ffd400";
    ctx.font = "700 10px ui-monospace, monospace";
    ctx.fillText(String(j + 1), x + 4, y + 11);
  });
}

/* ------------------------------------------------------------------ 마스크 목록 */
function renderMaskList() {
  const c = activeClip();
  const box = $("#maskList");
  box.innerHTML = "";
  if (!c || !c.masks.length) {
    box.innerHTML = '<p class="hint">지정한 영역이 없습니다.</p>';
    return;
  }
  c.masks.forEach((m, j) => {
    const el = document.createElement("div");
    el.className = "mask-item";
    const label = m.mode === "delogo" ? "주변픽셀" : m.mode === "blur" ? "흐리게" : "덮어쓰기";
    el.innerHTML = `<span>${j + 1}. ${label} · ${(m.h * 100).toFixed(0)}% 높이</span>`;
    if (m.mode === "box") {
      const inp = document.createElement("input");
      inp.placeholder = "덮을 한글 자막";
      inp.value = m.text || "";
      inp.style.width = "110px";
      inp.addEventListener("input", () => { m.text = inp.value; draw(); });
      el.appendChild(inp);
    }
    const del = document.createElement("button");
    del.className = "icon-btn del";
    del.textContent = "✕";
    del.addEventListener("click", () => {
      c.masks.splice(j, 1);
      renderMaskList(); draw();
    });
    el.appendChild(del);
    box.appendChild(el);
  });
}

/* ------------------------------------------------------------------ 캔버스 드래그 */
let dragStart = null;
let dragRect = null;

canvas.addEventListener("pointerdown", (e) => {
  const c = activeClip();
  if (!c) return;
  const r = canvas.getBoundingClientRect();
  dragStart = { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
  dragRect = null;
  canvas.setPointerCapture(e.pointerId);
});

canvas.addEventListener("pointermove", (e) => {
  if (!dragStart) return;
  const r = canvas.getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  dragRect = {
    x: Math.min(dragStart.x, x), y: Math.min(dragStart.y, y),
    w: Math.abs(x - dragStart.x), h: Math.abs(y - dragStart.y),
  };
  draw();
  if (dragRect.w > 0.005 && dragRect.h > 0.005) {
    ctx.strokeStyle = "#ff3b30";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(dragRect.x * CW, dragRect.y * CH, dragRect.w * CW, dragRect.h * CH);
    ctx.setLineDash([]);
  }
});

canvas.addEventListener("pointerup", () => {
  const c = activeClip();
  if (c && dragRect && dragRect.w > 0.01 && dragRect.h > 0.01) {
    c.masks.push({ ...dragRect, mode: state.maskMode, text: "", color: "0x000000" });
    if (state.maskMode === "box" && !c.masks[c.masks.length - 1].text) {
      // 덮어쓰기 모드는 바로 한글 자막을 물어본다
      const t = prompt("이 영역에 덮을 한글 자막 (비우면 검은 박스만)");
      if (t) c.masks[c.masks.length - 1].text = t;
    }
    renderMaskList();
  }
  dragStart = null; dragRect = null;
  draw();
});

/* ------------------------------------------------------------------ 이벤트 연결 */
function bindEvents() {
  const clipInput = $("#clipInput");
  $("#dropzone").addEventListener("click", () => clipInput.click());
  clipInput.addEventListener("change", () => uploadClips(clipInput.files));

  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove("over");
  }));
  dz.addEventListener("drop", (e) => {
    if (e.dataTransfer.files?.length) uploadClips(e.dataTransfer.files);
  });

  $("#zoom").addEventListener("input", (e) => {
    const c = activeClip(); if (!c) return;
    c.zoom = +e.target.value / 100;
    $("#zoomVal").textContent = e.target.value + "%";
    draw();
  });
  $("#panX").addEventListener("input", (e) => {
    const c = activeClip(); if (!c) return;
    c.pan_x = +e.target.value / 100;
    $("#panXVal").textContent = e.target.value;
    draw();
  });
  $("#panY").addEventListener("input", (e) => {
    const c = activeClip(); if (!c) return;
    c.pan_y = +e.target.value / 100;
    $("#panYVal").textContent = e.target.value;
    draw();
  });
  $("#resetTransform").addEventListener("click", () => {
    const c = activeClip(); if (!c) return;
    c.zoom = 1; c.pan_x = 0; c.pan_y = 0;
    $("#zoom").value = 100; $("#panX").value = 0; $("#panY").value = 0;
    $("#zoomVal").textContent = "100%"; $("#panXVal").textContent = "0"; $("#panYVal").textContent = "0";
    draw();
  });

  $$("#maskSeg .seg-btn").forEach((b) => b.addEventListener("click", () => {
    state.maskMode = b.dataset.mask;
    $$("#maskSeg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
  }));

  $$("#modeSeg .seg-btn").forEach((b) => b.addEventListener("click", () => {
    applyMode(b.dataset.mode); renderClipList(); saveLocal();
  }));

  // 제목 입력
  [
    ["title1", (v) => { state.title.lines[0] = v; }],
    ["title2", (v) => { state.title.lines[1] = v; }],
    ["badgeText", (v) => { state.title.badge_text = v; }],
    ["badgeNum", (v) => { state.title.badge_number_text = v; }],
    ["credit", (v) => { state.title.credit = v; }],
  ].forEach(([id, set]) => {
    const el = $("#" + id);
    el.addEventListener("input", () => { set(el.value); draw(); saveLocal(); });
  });

  $("#fontScale").addEventListener("input", (e) => {
    state.title.font_scale = +e.target.value / 100;
    $("#fontScaleVal").textContent = state.title.font_scale.toFixed(2).replace(/0$/, "") + "x";
    draw(); saveLocal();
  });
  $("#dim").addEventListener("input", (e) => {
    state.thumb.dim = +e.target.value / 100;
    $("#dimVal").textContent = e.target.value + "%";
    draw(); saveLocal();
  });

  // TTS / 오디오
  $$("#audioSeg .seg-btn").forEach((b) => b.addEventListener("click", () => {
    state.audio.source = b.dataset.audio;
    $$("#audioSeg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    $("#ttsBox").hidden = b.dataset.audio !== "tts";
    $("#fileBox").hidden = b.dataset.audio !== "file";
    if (b.dataset.audio === "clip") state.audio.audio_id = null;
    renderAudioList(); saveLocal();
  }));

  $("#ttsBtn").addEventListener("click", makeTts);
  $("#audioInput").addEventListener("change", uploadAudio);
  $("#volume").addEventListener("input", (e) => {
    state.audio.volume = +e.target.value / 100;
    $("#volumeVal").textContent = e.target.value + "%";
    saveLocal();
  });

  $("#subEnabled").addEventListener("change", (e) => { state.sub.enabled = e.target.checked; saveLocal(); });
  $("#subAutoBtn").addEventListener("click", makeSubtitles);
  $("#subInput").addEventListener("change", uploadSubtitle);

  // 썸네일
  $("#thumbSource").addEventListener("change", (e) => {
    state.thumb.source.type = e.target.value;
    $("#thumbTimeWrap").hidden = e.target.value !== "frame";
    $("#thumbImageWrap").hidden = e.target.value !== "image";
  });
  $("#thumbImageInput").addEventListener("change", uploadThumbImage);
  $("#thumbAsIs").addEventListener("change", (e) => { state.thumb.source.as_is = e.target.checked; draw(); });
  $("#thumbPrepend").addEventListener("change", (e) => {
    state.thumb.prepend = e.target.checked; updateTotals(); saveLocal();
  });
  $("#prependDur").addEventListener("change", (e) => {
    state.thumb.prependDur = Math.max(0.2, Math.min(5, +e.target.value || 1));
    updateTotals(); saveLocal();
  });
  $("#thumbBtn").addEventListener("click", makeThumbnail);

  $("#renderBtn").addEventListener("click", startRender);
  $("#playBtn").addEventListener("click", togglePlay);
}

function syncTitleInputs() {
  const t = state.title;
  $("#title1").value = t.lines[0] || "";
  $("#title2").value = t.lines[1] || "";
  $("#badgeText").value = t.badge_text || "";
  $("#badgeNum").value = t.badge_number_text || "";
  $("#credit").value = t.credit || "";
  $("#fontScale").value = Math.round((t.font_scale || 1) * 100);
  $("#fontScaleVal").textContent = (t.font_scale || 1).toFixed(2).replace(/0$/, "") + "x";
  $("#dim").value = Math.round((state.thumb.dim || 0) * 100);
  $("#dimVal").textContent = Math.round((state.thumb.dim || 0) * 100) + "%";
  $("#volume").value = Math.round((state.audio.volume ?? 1) * 100);
  $("#volumeVal").textContent = Math.round((state.audio.volume ?? 1) * 100) + "%";
  $("#subEnabled").checked = state.sub.enabled;
  $("#thumbPrepend").checked = state.thumb.prepend;
  $("#prependDur").value = state.thumb.prependDur;
  $$("#audioSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.audio === state.audio.source));
  $("#ttsBox").hidden = state.audio.source !== "tts";
  $("#fileBox").hidden = state.audio.source !== "file";
  $("#subStatus").textContent = state.sub.id ? "자막 준비됨 (id " + state.sub.id + ")" : "자막 없음";
  renderAudioList();
}

/* ------------------------------------------------------------------ 업로드 */
async function uploadClips(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  Array.from(files).forEach((f) => fd.append("files", f));
  toast("업로드 중… " + files.length + "개");
  try {
    const data = await api("/api/clips", { method: "POST", body: fd });
    toast((data.clips || []).length + "개 클립 추가");
    await refreshClips();
  } catch (e) {
    toast("업로드 실패: " + e.message, true);
  }
}

async function uploadAudio(e) {
  const f = e.target.files?.[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const meta = await api("/api/audio", { method: "POST", body: fd });
    state.audio.items.unshift(meta);
    state.audio.audio_id = meta.id;
    state.audio.source = "file";
    renderAudioList(); saveLocal();
    toast("외부 오디오 추가 (" + (meta.duration || 0).toFixed(1) + "초)");
  } catch (err) {
    toast("오디오 업로드 실패: " + err.message, true);
  }
  e.target.value = "";
}

async function uploadThumbImage(e) {
  const f = e.target.files?.[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const meta = await api("/api/images", { method: "POST", body: fd });
    state.thumb.source.type = "image";
    state.thumb.source.image_id = meta.id;
    state.thumb.imageName = meta.name;
    toast("썸네일 이미지 업로드 완료");
  } catch (err) {
    toast("이미지 업로드 실패: " + err.message, true);
  }
}

async function makeTts() {
  const text = $("#ttsText").value.trim();
  if (!text) { toast("읽을 대본을 입력하세요", true); return; }
  const btn = $("#ttsBtn");
  btn.disabled = true; btn.textContent = "만드는 중…";
  try {
    const meta = await api("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: $("#ttsVoice").value, rate: $("#ttsRate").value }),
    });
    state.audio.items.unshift(meta);
    state.audio.audio_id = meta.id;
    state.audio.source = "tts";
    $$("#audioSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.audio === "tts"));
    renderAudioList(); saveLocal();
    toast("TTS 생성 완료 (" + (meta.duration || 0).toFixed(1) + "초)");
  } catch (e) {
    toast("TTS 실패: " + e.message, true);
  }
  btn.disabled = false; btn.textContent = "음성 만들기";
}

function renderAudioList() {
  const box = $("#audioList");
  box.innerHTML = "";
  state.audio.items.slice(0, 6).forEach((a) => {
    const el = document.createElement("div");
    el.className = "audio-item" + (a.id === state.audio.audio_id ? " active" : "");
    el.innerHTML = `<span class="tag">${a.source === "edge-tts" ? "TTS" : "파일"}</span>
      <span class="dim" style="max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.name || ""}</span>
      <audio controls preload="none" src="/api/audio/${a.id}/file"></audio>`;
    el.addEventListener("click", (ev) => {
      if (ev.target.tagName === "AUDIO") return;
      state.audio.audio_id = a.id;
      state.audio.source = a.source === "edge-tts" ? "tts" : "file";
      $$("#audioSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.audio === state.audio.source));
      renderAudioList(); saveLocal();
    });
    box.appendChild(el);
  });
}

/* ------------------------------------------------------------------ 자막 */
async function uploadSubtitle(e) {
  const f = e.target.files?.[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const data = await api("/api/subtitles", { method: "POST", body: fd });
    state.sub.id = data.id;
    state.sub.enabled = true;
    $("#subEnabled").checked = true;
    $("#subStatus").textContent = `자막 준비됨 · ${data.segments}줄 (${f.name})`;
    saveLocal();
    toast("자막 적용 완료 (" + data.segments + "줄)");
  } catch (err) {
    toast("자막 실패: " + err.message, true);
  }
  e.target.value = "";
}

async function makeSubtitles() {
  if (!state.audio.audio_id) { toast("먼저 TTS를 만들거나 오디오를 올려주세요", true); return; }
  const btn = $("#subAutoBtn");
  btn.disabled = true; btn.textContent = "자막 만드는 중…";
  try {
    const data = await api("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio_id: state.audio.audio_id, word_level: $("#subWord").checked }),
    });
    state.sub.id = data.id;
    state.sub.enabled = true;
    $("#subEnabled").checked = true;
    $("#subStatus").textContent = `자동 자막 ${data.segments}줄 · ${data.language}`;
    saveLocal();
    toast("자동 자막 완성 (" + data.segments + "줄)");
  } catch (e) {
    toast(e.message, true);
  }
  btn.disabled = false; btn.textContent = "자동 자막 (whisper)";
}

/* ------------------------------------------------------------------ 프로젝트 JSON */
function buildProject() {
  return {
    mode: state.mode,
    width: OUT.w, height: OUT.h, fps: 30,
    clips: state.clips.map((c) => ({
      clip_id: c.id, start: +c.start, end: +c.end,
      zoom: +c.zoom, pan_x: +c.pan_x, pan_y: +c.pan_y, mute: !!c.mute,
      masks: (c.masks || []).map((m) => ({
        x: m.x, y: m.y, w: m.w, h: m.h, mode: m.mode, text: m.text || "", color: m.color || "0x000000",
      })),
    })),
    title: {
      enabled: true, preset: state.title.preset, lines: state.title.lines,
      badge_text: state.title.badge_text, badge_number_text: state.title.badge_number_text,
      credit: state.title.credit, font_scale: state.title.font_scale,
      dim: state.thumb.dim,
    },
    badges: { enabled: state.mode === "shopping" },
    audio: {
      source: state.audio.source, audio_id: state.audio.audio_id,
      volume: state.audio.volume,
    },
    subtitles: { enabled: state.sub.enabled, subtitle_id: state.sub.id },
    thumbnail: {
      prepend: state.thumb.prepend, prepend_duration: state.thumb.prependDur,
      dim: state.thumb.dim,
      source: {
        type: state.thumb.source.type,
        clip_id: state.thumb.source.clip_id || (activeClip() ? activeClip().id : null),
        time: state.thumb.source.time,
        image_id: state.thumb.source.image_id,
        as_is: state.thumb.source.as_is,
      },
    },
  };
}

/* ------------------------------------------------------------------ 썸네일 PNG */
async function makeThumbnail() {
  if (!state.clips.length) { toast("클립을 먼저 올려주세요", true); return; }
  const t = $("#thumbTime").value.trim();
  state.thumb.source.clip_id = activeClip()?.id || state.clips[0].id;
  state.thumb.source.time = t === "" ? null : parseFloat(t);
  const btn = $("#thumbBtn");
  btn.disabled = true; btn.textContent = "만드는 중…";
  try {
    const data = await api("/api/thumbnail", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: buildProject() }),
    });
    const url = data.download + "?t=" + Date.now();
    $("#thumbPreview").src = url;
    $("#thumbDownload").href = data.download;
    $("#thumbResult").hidden = false;
    toast("썸네일 PNG 완성");
  } catch (e) {
    toast("썸네일 실패: " + e.message, true);
  }
  btn.disabled = false; btn.textContent = "썸네일 PNG 만들기";
}

/* ------------------------------------------------------------------ 렌더 */
async function startRender() {
  if (!state.clips.length) { toast("클립을 먼저 올려주세요", true); return; }
  if ((state.audio.source === "tts" || state.audio.source === "file") && !state.audio.audio_id) {
    toast("음성 소스를 선택했는데 오디오가 없습니다. TTS를 만들거나 파일을 올려주세요.", true);
    return;
  }
  const btn = $("#renderBtn");
  btn.disabled = true; btn.textContent = "렌더링 중…";
  $("#downloadLink").hidden = true;
  $("#progressWrap").hidden = false;
  setProgress(0, "준비");
  try {
    const { job_id } = await api("/api/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: buildProject() }),
    });
    state.jobId = job_id;
    pollJob(job_id);
  } catch (e) {
    toast("렌더 시작 실패: " + e.message, true);
    btn.disabled = false; btn.textContent = "렌더링 시작";
    $("#progressWrap").hidden = true;
  }
}

function setProgress(pct, note) {
  $("#progressBar").style.width = Math.max(0, Math.min(100, pct)) + "%";
  $("#progressText").textContent = Math.round(pct) + "%";
  if (note) $("#progressText").title = note;
}

async function pollJob(jid) {
  const btn = $("#renderBtn");
  try {
    const job = await api("/api/jobs/" + jid);
    setProgress(job.progress || 0, job.note);
    if (job.status === "done") {
      btn.disabled = false; btn.textContent = "렌더링 시작";
      const link = $("#downloadLink");
      link.href = job.download || "/api/download/" + jid;
      link.hidden = false;
      toast("렌더 완료! MP4 다운로드 버튼을 눌러주세요.");
      return;
    }
    if (job.status === "error") {
      btn.disabled = false; btn.textContent = "렌더링 시작";
      toast(job.error || "렌더 실패", true);
      return;
    }
    setTimeout(() => pollJob(jid), 800);
  } catch (e) {
    btn.disabled = false; btn.textContent = "렌더링 시작";
    toast("진행률 조회 실패: " + e.message, true);
  }
}

/* ------------------------------------------------------------------ 재생 */
function togglePlay() {
  const c = activeClip();
  if (!c) return;
  if (state.playing) {
    video.pause(); state.playing = false;
    $("#playBtn").textContent = "▶ 재생";
    return;
  }
  video.currentTime = c.start + 0.02;
  video.muted = true;
  video.play().then(() => {
    state.playing = true;
    $("#playBtn").textContent = "⏸ 정지";
    tick();
  }).catch((e) => toast("재생 실패: " + e.message, true));
}

function tick() {
  const c = activeClip();
  if (!state.playing || !c) return;
  if (video.currentTime >= c.end || video.currentTime < c.start) {
    video.currentTime = c.start + 0.02;
  }
  draw();
  requestAnimationFrame(tick);
}

/* ------------------------------------------------------------------ 시작 */
document.addEventListener("DOMContentLoaded", init);
window.addEventListener("beforeunload", saveLocal);
