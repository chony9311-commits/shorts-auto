/* 일괄 제작(완전자동) 탭: 링크 붙여넣기 -> /api/batch -> 큐 진행률 폴링 */
(() => {
  const $ = (id) => document.getElementById(id);
  const STATUS = { queued: "대기", running: "제작 중", done: "완료", error: "실패", needs_manual: "직접 입력 필요" };
  let timer = null;

  // ---- 탭 전환
  document.querySelectorAll("#viewSeg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#viewSeg .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
      const batch = b.dataset.view === "batch";
      $("batchView").hidden = !batch;
      $("editorView").hidden = batch;
      const foot = document.querySelector("footer");
      if (foot) foot.hidden = batch;
      if (batch) { refresh(); startPoll(); } else stopPoll();
    });
  });

  $("bRate").addEventListener("input", () => { $("bRateV").textContent = "+" + $("bRate").value + "%"; });
  $("bCrop").addEventListener("input", () => { $("bCropV").textContent = $("bCrop").value + "%"; });

  fetch("/api/state").then((r) => r.json()).then((s) => {
    $("bTg").textContent = s.auto && s.auto.telegram
      ? "📨 텔레그램 봇 연결됨 — 봇에 링크를 보내도 같은 큐에서 제작돼요."
      : "텔레그램 연동: .env 에 TELEGRAM_BOT_TOKEN 을 넣고 서버를 다시 켜면 봇으로도 링크를 보낼 수 있어요.";
  }).catch(() => {});

  function toast(msg) {
    const t = $("toast"); if (!t) return alert(msg);
    t.textContent = msg; t.hidden = false; setTimeout(() => { t.hidden = true; }, 3500);
  }

  async function api(url, opt) {
    const r = await fetch(url, opt);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || "요청 실패");
    return j;
  }

  $("bGo").addEventListener("click", async () => {
    const text = $("bLinks").value.trim();
    if (!text) return toast("링크를 붙여 넣어 주세요.");
    $("bGo").disabled = true;
    try {
      const res = await api("/api/batch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text, voice: $("bVoice").value, rate: "+" + $("bRate").value + "%", crop_pct: +$("bCrop").value,
          subtitle_style: $("bSub").value, title_preset: $("bTitle").value, auto_search: $("bAuto").checked,
          extra_urls: $("bExtra").value.split(/\n+/).map((x) => x.trim()).filter(Boolean),
        }),
      });
      toast(res.items.length + "개 접수했어요");
      $("bLinks").value = "";
      refresh(); startPoll();
    } catch (e) { toast(e.message); } finally { $("bGo").disabled = false; }
  });

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function render(items) {
    const box = $("bList");
    if (!items.length) { box.innerHTML = '<p class="hint">아직 없어요.</p>'; return; }
    box.innerHTML = items.slice().reverse().map((it) => {
      let body = "";
      if (it.status === "running" || it.status === "queued")
        body = `<div class="bar"><i style="width:${it.progress}%"></i></div><div class="msg">${esc(it.note)} · ${Math.round(it.progress)}%</div>`;
      if (it.status === "done") {
        body = `<div class="msg">${(it.duration || 0).toFixed(1)}초 · ${esc((it.script || []).length)}줄</div>` +
          `<div class="script">${esc((it.script || []).join(" / "))}</div>` +
          (it.warnings && it.warnings.length ? `<div class="msg">⚠️ ${esc(it.warnings.join(" · "))}</div>` : "") +
          `<div class="acts"><a class="btn btn-primary" href="${it.download}" download>MP4 받기</a></div>`;
      }
      if (it.status === "error")
        body = `<div class="msg">${esc(it.error)}</div><div class="acts"><button class="btn" data-act="retry" data-id="${it.id}">다시 시도</button></div>`;
      if (it.status === "needs_manual")
        body = `<div class="msg">${esc(it.error)}</div><div class="manual" data-id="${it.id}">
          <input class="m-name" placeholder="상품명"><input class="m-feat" placeholder="특징 (쉼표로 구분)"><input class="m-price" placeholder="가격 (선택) 19,800원">
          <div class="acts"><button class="btn btn-primary" data-act="manual" data-id="${it.id}">이 내용으로 제작</button></div></div>`;
      const del = it.status !== "running" ? `<button class="btn" data-act="del" data-id="${it.id}">삭제</button>` : "";
      return `<div class="bitem ${it.status}"><div class="top"><span>${esc(it.label)}</span><span class="st">${STATUS[it.status] || it.status}</span></div>${body}<div class="acts">${del}</div></div>`;
    }).join("");
  }

  $("bList").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const id = b.dataset.id;
    try {
      if (b.dataset.act === "del") await api("/api/batch/" + id, { method: "DELETE" });
      if (b.dataset.act === "retry") await api(`/api/batch/${id}/retry`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (b.dataset.act === "manual") {
        const w = b.closest(".manual");
        const name = w.querySelector(".m-name").value.trim();
        if (!name) return toast("상품명을 적어 주세요.");
        await api(`/api/batch/${id}/retry`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, features: w.querySelector(".m-feat").value, price: w.querySelector(".m-price").value }) });
      }
      refresh(); startPoll();
    } catch (err) { toast(err.message); }
  });

  async function refresh() {
    try {
      const { items } = await api("/api/batch");
      const a = document.activeElement;
      if (!(a && a.tagName === "INPUT" && $("bList").contains(a))) render(items);   // 입력 중이면 덮어쓰지 않음
      if (!items.some((i) => i.status === "queued" || i.status === "running")) stopPoll();
    } catch (_) { /* 서버 재시작 중 */ }
  }
  function startPoll() { if (!timer) timer = setInterval(refresh, 1200); }
  function stopPoll() { clearInterval(timer); timer = null; }
})();
