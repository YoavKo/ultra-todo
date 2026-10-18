// ultra-todo UI: drag-and-drop, sync buttons, tag input, comments.
(() => {
  const T = window.I18N || {};
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content ||
    (document.cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "";

  async function api(url, body, method = "POST") {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, Accept: "application/json" },
      body: method === "GET" ? undefined : JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({ error: res.statusText }));
    if (!res.ok) throw Object.assign(new Error(data.error || T.error), { data, status: res.status });
    return data;
  }

  function toast(msg, kind = "", { files = [], actions = [], sticky = false } = {}) {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.dir = "auto";
    const p = document.createElement("div");
    p.textContent = msg;
    el.append(p);
    if (files.length) {
      const ul = document.createElement("ul");
      files.forEach((f) => { const li = document.createElement("li"); li.textContent = f; ul.append(li); });
      el.append(ul);
    }
    if (actions.length) {
      const row = document.createElement("div");
      row.className = "actions";
      actions.forEach(([label, fn]) => {
        const b = document.createElement("button");
        b.className = "btn"; b.textContent = label;
        b.onclick = () => { el.remove(); fn(); };
        row.append(b);
      });
      el.append(row);
    }
    el.addEventListener("click", (e) => { if (e.target === el || e.target === p) el.remove(); });
    document.getElementById("toasts").append(el);
    if (!sticky && !actions.length && !files.length) setTimeout(() => el.remove(), 5000);
  }

  // ---- sync status + buttons ---------------------------------------
  const btnUpdate = document.getElementById("btn-update");
  const btnSave = document.getElementById("btn-save");
  const badge = document.getElementById("changes-badge");
  const statusEl = document.getElementById("sync-status");

  function renderStatus(s) {
    if (!s) return;
    let txt = `⎇ ${s.branch}`;
    if (s.upstream) {
      if (s.ahead) txt += ` ↑${s.ahead}`;
      if (s.behind) txt += ` ↓${s.behind}`;
    } else txt += ` (${T.noUpstream})`;
    statusEl.textContent = txt;
    statusEl.title = s.upstream || "";
    badge.hidden = !s.changes;
    badge.textContent = s.changes || "";
    btnUpdate.disabled = !s.upstream;
    btnSave.disabled = !s.has_remote && !s.changes;
    btnSave.dataset.changes = s.changes;
  }
  const refreshStatus = () => api("/api/sync-status", null, "GET").then(renderStatus).catch(() => {});

  async function busy(btn, fn) {
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = T.working;
    try { await fn(); } finally { btn.innerHTML = label; refreshStatus(); }
  }

  function handleSyncError(e, retry) {
    const d = e.data || {};
    const actions = [];
    if (d.code === "diverged") actions.push([T.rebase, () => doUpdate(true)]);
    if (d.code === "no_upstream" && retry) actions.push([T.publish, retry]);
    toast(d.error || e.message, "error", { files: d.files || [], actions, sticky: true });
    if (d.detail && !d.files?.length && d.code !== "diverged") console.warn(d.detail);
  }

  function doUpdate(rebase = false) {
    return busy(btnUpdate, async () => {
      try {
        const r = await api("/sync/pull", { rebase });
        toast(r.message, "ok");
        if (r.summary && (r.summary.full || r.summary.tasks_added || r.summary.tasks_updated ||
            r.summary.tasks_deleted || r.summary.comments_changed)) setTimeout(() => location.reload(), 700);
      } catch (e) { handleSyncError(e); }
    });
  }

  function doSave(message, setUpstream = false) {
    return busy(btnSave, async () => {
      try {
        const r = await api("/sync/save", { message, set_upstream: setUpstream });
        toast(r.message, "ok");
      } catch (e) { handleSyncError(e, () => doSave(message, true)); }
    });
  }

  btnUpdate?.addEventListener("click", () => doUpdate(false));
  btnSave?.addEventListener("click", () => {
    const dlg = document.getElementById("save-dialog");
    const n = +btnSave.dataset.changes || 0;
    document.getElementById("save-summary").textContent =
      n ? T.changes.replace("{n}", n) : T.noChanges;
    document.getElementById("save-message").value = "";
    dlg.showModal();
    dlg.onclose = () => { if (dlg.returnValue === "ok") doSave(document.getElementById("save-message").value); };
  });
  if (statusEl) { refreshStatus(); setInterval(refreshStatus, 30000); }

  // ---- board drag & drop -------------------------------------------
  let dragged = null;
  document.querySelectorAll(".card[draggable]").forEach((card) => {
    card.addEventListener("dragstart", (e) => {
      dragged = card; card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id);
    });
    card.addEventListener("dragend", () => { card.classList.remove("dragging"); dragged = null; });
  });
  document.querySelectorAll("[data-dropzone]").forEach((zone) => {
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", async (e) => {
      e.preventDefault(); zone.classList.remove("over");
      if (!dragged) return;
      const card = dragged, from = card.parentElement;
      const status = zone.closest(".column").dataset.status;
      if (from === zone) return;
      zone.prepend(card);
      try {
        const r = await api(`/task/${encodeURIComponent(card.dataset.id)}/status`, { status });
        if (r.commit_error) toast(r.commit_error, "error");
        updateCounts(); refreshStatus();
      } catch (err) { from.prepend(card); toast(err.message, "error", { sticky: true }); }
    });
  });
  function updateCounts() {
    document.querySelectorAll(".column").forEach((c) => {
      const n = c.querySelectorAll(".card").length;
      const span = c.querySelector("h2 .muted");
      if (span) span.textContent = n;
    });
  }

  // ---- tag input chips ---------------------------------------------
  document.querySelectorAll("[data-tag-input]").forEach((box) => {
    const input = box.querySelector("input[name=tags_text]");
    const has = (v) => [...box.querySelectorAll("input[name=tags]")]
      .some((i) => i.value.toLocaleLowerCase() === v.toLocaleLowerCase());
    function add(v) {
      v = v.trim().replace(/\s+/g, " ");
      if (!v || has(v)) return;
      const chip = document.createElement("span");
      chip.className = "chip"; chip.dir = "auto"; chip.textContent = v;
      const hidden = Object.assign(document.createElement("input"), { type: "hidden", name: "tags", value: v });
      const x = Object.assign(document.createElement("button"), { type: "button", className: "x", textContent: "×" });
      chip.append(hidden, x);
      box.insertBefore(chip, input);
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault(); add(input.value); input.value = "";
      } else if (e.key === "Backspace" && !input.value) {
        box.querySelectorAll(".chip").length && box.querySelectorAll(".chip")[box.querySelectorAll(".chip").length - 1].remove();
      }
    });
    input.addEventListener("change", () => { if (input.value && !input.value.includes(",")) { add(input.value); input.value = ""; } });
    box.addEventListener("click", (e) => { if (e.target.classList.contains("x")) e.target.parentElement.remove(); });
  });

  // ---- comments: edit toggle, paste/drop files, confirm ------------
  document.querySelectorAll("[data-edit-toggle]").forEach((b) => b.addEventListener("click", () => {
    const art = b.closest(".comment");
    const form = art.querySelector("form.edit");
    form.hidden = !form.hidden;
    art.querySelector(".body").hidden = !form.hidden;
    if (!form.hidden) form.querySelector("textarea").focus();
  }));

  document.querySelectorAll("[data-paste-files]").forEach((form) => {
    const fileInput = form.querySelector("input[type=file]");
    const addFiles = (files) => {
      const dt = new DataTransfer();
      [...fileInput.files, ...files].forEach((f) => dt.items.add(f));
      fileInput.files = dt.files;
    };
    form.addEventListener("paste", (e) => {
      const files = [...(e.clipboardData?.files || [])];
      if (files.length) { e.preventDefault(); addFiles(files.map((f, i) =>
        f.name && f.name !== "image.png" ? f :
          new File([f], `pasted-${Date.now()}-${i}.${(f.type.split("/")[1] || "png")}`, { type: f.type }))); }
    });
    form.addEventListener("dragover", (e) => { e.preventDefault(); form.classList.add("drop"); });
    form.addEventListener("dragleave", () => form.classList.remove("drop"));
    form.addEventListener("drop", (e) => {
      e.preventDefault(); form.classList.remove("drop");
      if (e.dataTransfer.files.length) addFiles([...e.dataTransfer.files]);
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => { if (!confirm(T.confirmDelete)) e.preventDefault(); }));
})();
