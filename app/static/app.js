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
  tool: "move",     // 캔버스 조작 모드: move(이동) | mask(가림) | select(선택)
  safe: false,      // 9:16 안전영역 오버레이
  selMask: -1,      // 선택 모드에서 고른 가림 박스 번호
};

/* 9:16 안전영역(1080×1920 기준 px) — 플랫폼 UI가 덮는 구간 */
const SAFE = { top: 220, bottom: 420, subBottom: 220, subH: 190 };
const SNAP_MARGIN = 44 / OUT.w;           // 좌우 여백 스냅선(제목바 margin_x와 동일)

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
      sub: state.sub, safe: state.safe,
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
    if (typeof d.safe === "boolean") state.safe = d.safe;
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
  setTool(state.tool);
  $("#safeToggle").checked = state.safe;
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
        const nv = k === "start" ? Math.min(v, c.end - 0.2) : Math.max(v, c.start + 0.2);
        if (nv === c[k]) return;            // 값이 안 바뀌었으면 되돌리기 기록도 남기지 않는다
        pushUndo();
        c[k] = nv;
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
  state.selMask = -1;
  syncTransformUI(c);
  renderMaskList();
  draw();
}

/* 줌·이동 슬라이더를 클립 값에 맞춰 갱신 */
function syncTransformUI(c) {
  const z = $("#zoom");
  z.value = Math.round((c.zoom || 1) * 100);
  $("#panX").value = Math.round((c.pan_x || 0) * 100);
  $("#panY").value = Math.round((c.pan_y || 0) * 100);
  $("#zoomVal").textContent = z.value + "%";
  $("#panXVal").textContent = $("#panX").value;
  $("#panYVal").textContent = $("#panY").value;
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
  drawOverlays(c);
  updateSafeWarn();
  updateTimeRead();
}

let decorRects = []; // 안전영역 경고용: 제목바·배지·출처 태그가 차지한 캔버스 좌표

function drawDecor() {
  decorRects = [];
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
  decorRects.push({ name: "제목바", x: 0, y: 0, w: CW, h: barH });

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
    decorRects.push({ name: right ? "번호 배지" : "금액 배지", x: bx, y: by, w: bw, h: bh });
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
    decorRects.push({ name: "출처 태그", x: CW - tw - 64 * S, y: CH - f2 * 1.7 - 90 * S, w: tw + 48 * S, h: f2 * 1.7 });
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
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("input,button")) return;
      state.selMask = j; setTool("select");
    });
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
      pushUndo();
      c.masks.splice(j, 1);
      state.selMask = -1;
      renderMaskList(); draw();
    });
    el.appendChild(del);
    box.appendChild(el);
  });
}

/* ------------------------------------------------------------------ 되돌리기 (가림 박스 · 컷 구간) */
const undoStack = [];
const redoStack = [];

function takeSnapshot() {
  return state.clips.map((c) => ({
    id: c.id, start: c.start, end: c.end, masks: JSON.parse(JSON.stringify(c.masks || [])),
  }));
}

function pushUndo() {
  const snap = takeSnapshot();
  const top = undoStack[undoStack.length - 1];
  if (top && JSON.stringify(top) === JSON.stringify(snap)) return; // 같은 상태 중복 저장 방지
  undoStack.push(snap);
  if (undoStack.length > 100) undoStack.shift();
  redoStack.length = 0;
  updateUndoUI();
}

function restoreSnapshot(snap) {
  const byId = new Map(snap.map((x) => [x.id, x]));
  state.clips.forEach((c) => {
    const x = byId.get(c.id);
    if (!x) return;
    c.start = x.start; c.end = x.end;
    c.masks = JSON.parse(JSON.stringify(x.masks));
  });
  state.selMask = -1;
  renderClipList(); updateTotals(); draw();
}

function undo() {
  if (!undoStack.length) { toast("되돌릴 작업이 없습니다"); return; }
  redoStack.push(takeSnapshot());
  restoreSnapshot(undoStack.pop());
  updateUndoUI();
}

function redo() {
  if (!redoStack.length) { toast("다시 실행할 작업이 없습니다"); return; }
  undoStack.push(takeSnapshot());
  restoreSnapshot(redoStack.pop());
  updateUndoUI();
}

function updateUndoUI() {
  $("#undoBtn").disabled = !undoStack.length;
  $("#redoBtn").disabled = !redoStack.length;
}

/* ------------------------------------------------------------------ 조작 모드 */
const TOOL_HINTS = {
  move: "<b>드래그</b>로 화면 위치 이동 · <b>⌘+휠</b>(트랙패드는 <b>핀치</b>)로 확대·축소",
  mask: "드래그해서 <b>지울 영역</b>을 그립니다 (화면 중앙·좌우 여백에 자석처럼 붙어요)",
  select: "박스를 <b>클릭해 선택</b> → 끌어서 이동, 모서리로 크기 조절, <b>Delete</b>로 삭제",
};

function setTool(tool) {
  state.tool = tool;
  $$("#toolSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  canvas.classList.remove("tool-move", "tool-mask", "tool-select", "dragging");
  canvas.classList.add("tool-" + tool);
  $("#toolHint").innerHTML = TOOL_HINTS[tool];
  if (tool !== "select") state.selMask = -1;
  draw();
}

/* ------------------------------------------------------------------ 스냅 · 히트 테스트 */
const SNAP_XS = [SNAP_MARGIN, 0.5, 1 - SNAP_MARGIN]; // 좌 여백 · 중앙 · 우 여백
const SNAP_YS = [0.5];
let guides = { x: [], y: [] };

function nearest(v, targets, thr) {
  let best = null;
  for (const t of targets) {
    if (Math.abs(v - t) <= thr && (best === null || Math.abs(v - t) < Math.abs(v - best))) best = t;
  }
  return best;
}

/* 점 하나(그리기·크기조절의 움직이는 모서리)를 스냅 */
function snapPoint(px, py, thr) {
  const sx = nearest(px, SNAP_XS, thr.x);
  const sy = nearest(py, SNAP_YS, thr.y);
  guides = { x: sx === null ? [] : [sx], y: sy === null ? [] : [sy] };
  return { x: sx === null ? px : sx, y: sy === null ? py : sy };
}

/* 박스 이동: 왼쪽·가운데·오른쪽(위·가운데·아래) 중 가장 가까운 선에 붙인다 */
function snapMove(m, thr) {
  let dx = null, dy = null, gx = null, gy = null;
  for (const off of [0, m.w / 2, m.w]) {
    for (const t of SNAP_XS) {
      const d = t - (m.x + off);
      if (Math.abs(d) <= thr.x && (dx === null || Math.abs(d) < Math.abs(dx))) { dx = d; gx = t; }
    }
  }
  for (const off of [0, m.h / 2, m.h]) {
    for (const t of SNAP_YS) {
      const d = t - (m.y + off);
      if (Math.abs(d) <= thr.y && (dy === null || Math.abs(d) < Math.abs(dy))) { dy = d; gy = t; }
    }
  }
  guides = { x: gx === null ? [] : [gx], y: gy === null ? [] : [gy] };
  if (dx !== null) m.x += dx;
  if (dy !== null) m.y += dy;
}

function canvasPos(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height,
    rw: r.width, rh: r.height,
  };
}

const clamp01 = (v) => Math.min(1, Math.max(0, v));

/* 선택 모드: 어느 모서리/몸통을 잡았는지 */
function hitMask(c, pos) {
  const hx = 10 / pos.rw, hy = 10 / pos.rh;
  const masks = c.masks || [];
  // 이미 선택된 박스의 모서리를 먼저 본다
  const sel = masks[state.selMask];
  if (sel) {
    const corners = { nw: [sel.x, sel.y], ne: [sel.x + sel.w, sel.y], sw: [sel.x, sel.y + sel.h], se: [sel.x + sel.w, sel.y + sel.h] };
    for (const [name, [cx, cy]] of Object.entries(corners)) {
      if (Math.abs(pos.x - cx) <= hx && Math.abs(pos.y - cy) <= hy) return { idx: state.selMask, corner: name };
    }
  }
  for (let j = masks.length - 1; j >= 0; j--) {
    const m = masks[j];
    if (pos.x >= m.x && pos.x <= m.x + m.w && pos.y >= m.y && pos.y <= m.y + m.h) return { idx: j, corner: null };
  }
  return null;
}

/* ------------------------------------------------------------------ 캔버스 제스처 */
let gesture = null;

/* 화면 이동: 드래그한 만큼 영상이 따라오게 pan 값을 역산한다.
 * render.py compute_crop 과 같은 식: x = slack * (0.5 + pan/2)  ->  pan = (x/slack - 0.5) * 2 */
function panFromDrag(c, g, e) {
  const crop = g.crop;
  if (!crop) return;
  const dxFrac = (e.clientX - g.sx) / g.rw;
  const dyFrac = (e.clientY - g.sy) / g.rh;
  if (g.slackX > 0) {
    const nx = Math.min(g.slackX, Math.max(0, g.crop.x - dxFrac * crop.w));
    c.pan_x = Math.min(1, Math.max(-1, (nx / g.slackX - 0.5) * 2));
  }
  if (g.slackY > 0) {
    const ny = Math.min(g.slackY, Math.max(0, g.crop.y - dyFrac * crop.h));
    c.pan_y = Math.min(1, Math.max(-1, (ny / g.slackY - 0.5) * 2));
  }
  syncTransformUI(c);
}

function setZoom(c, z) {
  c.zoom = Math.min(2, Math.max(1, z));
  syncTransformUI(c);
  draw();
}

canvas.addEventListener("pointerdown", (e) => {
  const c = activeClip();
  if (!c || e.button > 0) return;
  const pos = canvasPos(e);
  canvas.setPointerCapture(e.pointerId);

  if (state.tool === "move") {
    const crop = computeCrop(video.videoWidth, video.videoHeight, c.zoom, c.pan_x, c.pan_y);
    if (!crop) return;
    gesture = {
      type: "pan", sx: e.clientX, sy: e.clientY, rw: pos.rw, rh: pos.rh, crop,
      slackX: Math.max(0, video.videoWidth - crop.w), slackY: Math.max(0, video.videoHeight - crop.h),
    };
    canvas.classList.add("dragging");
  } else if (state.tool === "mask") {
    const thr = { x: 7 / pos.rw, y: 7 / pos.rh };
    const p0 = snapPoint(clamp01(pos.x), clamp01(pos.y), thr);
    gesture = { type: "draw", start: p0, rect: null };
  } else {
    const hit = hitMask(c, pos);
    if (!hit) { state.selMask = -1; gesture = null; renderMaskList(); draw(); return; }
    state.selMask = hit.idx;
    pushUndo();
    const m = c.masks[hit.idx];
    gesture = {
      type: hit.corner ? "resize" : "move", idx: hit.idx, corner: hit.corner,
      start: { x: pos.x, y: pos.y }, orig: { x: m.x, y: m.y, w: m.w, h: m.h },
    };
    renderMaskList(); draw();
  }
});

canvas.addEventListener("pointermove", (e) => {
  const c = activeClip();
  if (!c) return;
  const pos = canvasPos(e);
  const thr = { x: 7 / pos.rw, y: 7 / pos.rh };

  if (!gesture) {
    if (state.tool === "select") {   // 마우스 올렸을 때 커서 모양
      const hit = hitMask(c, pos);
      canvas.style.cursor = !hit ? "default"
        : hit.corner ? (hit.corner === "nw" || hit.corner === "se" ? "nwse-resize" : "nesw-resize") : "move";
    } else canvas.style.cursor = "";
    return;
  }

  if (gesture.type === "pan") {
    panFromDrag(c, gesture, e);
    draw();
  } else if (gesture.type === "draw") {
    const p1 = snapPoint(clamp01(pos.x), clamp01(pos.y), thr);
    gesture.rect = {
      x: Math.min(gesture.start.x, p1.x), y: Math.min(gesture.start.y, p1.y),
      w: Math.abs(p1.x - gesture.start.x), h: Math.abs(p1.y - gesture.start.y),
    };
    draw();
    if (gesture.rect.w > 0.005 && gesture.rect.h > 0.005) {
      const r = gesture.rect;
      ctx.strokeStyle = "#ff3b30";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(r.x * CW, r.y * CH, r.w * CW, r.h * CH);
      ctx.setLineDash([]);
    }
  } else if (gesture.type === "move") {
    const m = c.masks[gesture.idx];
    const o = gesture.orig;
    m.x = Math.min(1 - o.w, Math.max(0, o.x + (pos.x - gesture.start.x)));
    m.y = Math.min(1 - o.h, Math.max(0, o.y + (pos.y - gesture.start.y)));
    snapMove(m, thr);
    m.x = Math.min(1 - m.w, Math.max(0, m.x));
    m.y = Math.min(1 - m.h, Math.max(0, m.y));
    draw();
  } else if (gesture.type === "resize") {
    const m = c.masks[gesture.idx];
    const o = gesture.orig;
    const p1 = snapPoint(clamp01(pos.x), clamp01(pos.y), thr);
    // 잡은 모서리의 반대쪽 모서리는 고정
    const fx = gesture.corner.includes("w") ? o.x + o.w : o.x;
    const fy = gesture.corner.includes("n") ? o.y + o.h : o.y;
    m.x = Math.min(fx, p1.x); m.y = Math.min(fy, p1.y);
    m.w = Math.max(0.01, Math.abs(p1.x - fx)); m.h = Math.max(0.01, Math.abs(p1.y - fy));
    draw();
  }
});

function endGesture() {
  const c = activeClip();
  const g = gesture;
  gesture = null;
  guides = { x: [], y: [] };
  canvas.classList.remove("dragging");
  if (!c || !g) { draw(); return; }

  if (g.type === "draw" && g.rect && g.rect.w > 0.01 && g.rect.h > 0.01) {
    pushUndo();
    c.masks.push({ ...g.rect, mode: state.maskMode, text: "", color: "0x000000" });
    state.selMask = c.masks.length - 1;
    if (state.maskMode === "box") {
      // 덮어쓰기 모드는 바로 한글 자막을 물어본다
      const t = prompt("이 영역에 덮을 한글 자막 (비우면 검은 박스만)");
      if (t) c.masks[c.masks.length - 1].text = t;
    }
    renderMaskList();
  } else if (g.type === "move" || g.type === "resize") {
    renderMaskList();
  }
  draw();
}
canvas.addEventListener("pointerup", endGesture);
canvas.addEventListener("pointercancel", endGesture);

/* 휠 + ⌘(Ctrl) / 트랙패드 핀치 → 확대. 크롬·파이어폭스의 핀치는 ctrlKey 달린 wheel 로 들어온다 */
canvas.addEventListener("wheel", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  e.preventDefault();
  const c = activeClip();
  if (!c) return;
  const k = e.metaKey ? 0.0025 : 0.01;
  setZoom(c, (c.zoom || 1) * Math.exp(-e.deltaY * k));
}, { passive: false });

/* 사파리 핀치 */
let gestureZoom0 = 1;
canvas.addEventListener("gesturestart", (e) => {
  e.preventDefault();
  gestureZoom0 = activeClip()?.zoom || 1;
});
canvas.addEventListener("gesturechange", (e) => {
  e.preventDefault();
  const c = activeClip();
  if (c) setZoom(c, gestureZoom0 * e.scale);
});

/* ------------------------------------------------------------------ 오버레이 (안전영역 · 스냅선 · 선택 핸들) */
function drawOverlays(c) {
  ctx.save();
  ctx.textBaseline = "top";
  ctx.textAlign = "left";

  if (state.safe) {
    const top = SAFE.top * S, bot = SAFE.bottom * S;
    ctx.fillStyle = "rgba(255, 70, 70, 0.14)";
    ctx.fillRect(0, 0, CW, top);
    ctx.fillRect(0, CH - bot, CW, bot);
    ctx.strokeStyle = "rgba(255,255,255,0.85)";
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(0, top); ctx.lineTo(CW, top);
    ctx.moveTo(0, CH - bot); ctx.lineTo(CW, CH - bot);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,255,255,0.9)";
    ctx.font = "700 10px ui-monospace, monospace";
    ctx.fillText(`상단 ${SAFE.top}px · 플랫폼 UI`, 6, top - 14);
    ctx.fillText(`하단 ${SAFE.bottom}px · 캡션·버튼`, 6, CH - bot + 4);
    // 자막 띠(MarginV 220) 위치도 얇게 표시
    const subTop = CH - (SAFE.subBottom + SAFE.subH) * S, subBot = CH - SAFE.subBottom * S;
    ctx.strokeStyle = "rgba(120, 200, 255, 0.7)";
    ctx.setLineDash([2, 4]);
    ctx.strokeRect(0.5, subTop, CW - 1, subBot - subTop);
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(120, 200, 255, 0.95)";
    ctx.fillText("자막 위치", 6, subTop + 3);
  }

  // 스냅 가이드선
  if (gesture && (guides.x.length || guides.y.length)) {
    ctx.strokeStyle = "#00e5ff";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    guides.x.forEach((gx) => { ctx.moveTo(gx * CW, 0); ctx.lineTo(gx * CW, CH); });
    guides.y.forEach((gy) => { ctx.moveTo(0, gy * CH); ctx.lineTo(CW, gy * CH); });
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // 선택 모드: 선택된 박스 테두리 + 모서리 핸들
  if (state.tool === "select") {
    const m = (c.masks || [])[state.selMask];
    if (m) {
      const x = m.x * CW, y = m.y * CH, w = m.w * CW, h = m.h * CH;
      ctx.strokeStyle = "#00e5ff";
      ctx.lineWidth = 2;
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = "#00a5bd";
      ctx.lineWidth = 1.5;
      [[x, y], [x + w, y], [x, y + h], [x + w, y + h]].forEach(([hx, hy]) => {
        ctx.fillRect(hx - 4, hy - 4, 8, 8);
        ctx.strokeRect(hx - 4, hy - 4, 8, 8);
      });
    }
  }
  ctx.restore();
}

/* 안전영역 경고: 켜져 있을 때만, 요소가 위험 구간·자막 띠에 걸리면 안내 */
let lastWarn = "";
function updateSafeWarn() {
  const box = $("#safeWarn");
  const msgs = [];
  if (state.safe) {
    const botTop = CH - SAFE.bottom * S;
    const subTop = CH - (SAFE.subBottom + SAFE.subH) * S;
    decorRects.forEach((r) => {
      if (r.name !== "제목바" && r.y + r.h > botTop) msgs.push(`${r.name}가 하단 ${SAFE.bottom}px 위험 구간에 걸려요`);
      if (r.name === "제목바" && r.y + r.h > subTop) msgs.push("제목바가 자막 위치까지 내려와요 (겹침)");
      else if (r.name !== "제목바" && r.y < subTop + (SAFE.subH * S) && r.y + r.h > subTop) msgs.push(`${r.name}가 자막 위치와 겹쳐요`);
    });
  }
  const text = Array.from(new Set(msgs)).join("\n");
  if (text === lastWarn) return;
  lastWarn = text;
  box.hidden = !text;
  box.innerHTML = text.split("\n").map((m) => "⚠ " + m).join("<br>");
}

/* 현재 재생 위치 표시 (←→ 이동 확인용) */
function updateTimeRead() {
  const c = activeClip();
  const el = $("#timeRead");
  if (!el) return;
  el.textContent = c ? `${(video.currentTime || 0).toFixed(1)} / ${(+c.end).toFixed(1)}초` : "";
}

/* ------------------------------------------------------------------ 키보드 단축키 */
function seekBy(delta) {
  const c = activeClip();
  if (!c) return;
  if (state.playing) togglePlay();
  const t = Math.min(c.end - 0.02, Math.max(c.start, (video.currentTime || c.start) + delta));
  video.currentTime = t;
}

document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
  const mod = e.metaKey || e.ctrlKey;

  if (mod && !typing && e.key.toLowerCase() === "z") {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
    return;
  }
  if (mod && !typing && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (typing || mod || e.altKey) return;

  const k = e.key;
  if (k === "ArrowLeft" || k === "ArrowRight") {
    e.preventDefault();
    seekBy((k === "ArrowLeft" ? -0.1 : 0.1) * (e.shiftKey ? 10 : 1));
  } else if (k === "," || k === ".") {
    e.preventDefault();
    const next = state.active + (k === "." ? 1 : -1);
    if (next >= 0 && next < state.clips.length) setActive(next);
  } else if (k === " " || e.code === "Space") {
    e.preventDefault();
    togglePlay();
  } else if (k === "Delete" || k === "Backspace") {
    const c = activeClip();
    if (c && state.tool === "select" && c.masks[state.selMask]) {
      e.preventDefault();
      pushUndo();
      c.masks.splice(state.selMask, 1);
      state.selMask = -1;
      renderMaskList(); draw();
    }
  } else if (k === "Escape") {
    state.selMask = -1; draw();
  } else if (k.toLowerCase() === "v") setTool("move");
  else if (k.toLowerCase() === "m") setTool("mask");
  else if (k.toLowerCase() === "s") setTool("select");
});

video.addEventListener("seeked", () => draw());

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
    setTool("mask");
  }));

  $$("#toolSeg .seg-btn").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("#safeToggle").addEventListener("change", (e) => { state.safe = e.target.checked; draw(); saveLocal(); });
  $("#undoBtn").addEventListener("click", undo);
  $("#redoBtn").addEventListener("click", redo);
  updateUndoUI();

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
