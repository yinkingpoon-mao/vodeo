const API = "";

const el = (id) => document.getElementById(id);
const cards = {
  setup: el("setup-card"),
  progress: el("progress-card"),
  review: el("review-card"),
  render: el("render-card"),
  done: el("done-card"),
  error: el("error-card"),
};

function showOnly(name) {
  Object.entries(cards).forEach(([key, node]) => {
    node.classList.toggle("hidden", key !== name);
  });
}

const STATUS_LABEL = {
  queued: ["準備緊…", 5],
  extracting_audio: ["抽緊音軌…", 15],
  transcribing: ["轉緊做文字 (Whisper)…", 35],
  analyzing_audio: ["分析緊音量變化…", 55],
  picking_highlights: ["揀緊邊段音量最高…", 70],
  cutting_previews: ["剪緊預覽片段…", 85],
  awaiting_review: ["等緊你揀選…", 100],
  rendering: ["合併緊最終精華片…", 100],
  done: ["完成", 100],
  error: ["出錯咗", 0],
};

let currentJobId = null;
let currentHighlights = [];

const fileInput = el("file-input");
const fileLabel = el("file-label");
const dropzone = el("dropzone");
const uploadBtn = el("upload-btn");

let selectedFile = null;
const uploadHint = el("upload-hint");

function updateUploadEnabled() {
  const ready = Boolean(selectedFile);
  uploadBtn.classList.toggle("dim", !ready);
  if (ready) uploadHint.textContent = "";
}

fileInput.addEventListener("change", () => {
  selectedFile = fileInput.files[0] || null;
  fileLabel.textContent = selectedFile ? selectedFile.name : "揀影片檔案 (或拖拽到呢度)";
  updateUploadEnabled();
});

["dragover", "dragenter"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
  })
);
dropzone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) {
    selectedFile = f;
    fileInput.files = e.dataTransfer.files;
    fileLabel.textContent = f.name;
    updateUploadEnabled();
  }
});

uploadBtn.addEventListener("click", async () => {
  if (!selectedFile) {
    uploadHint.textContent = "請先提供：影片檔案";
    return;
  }

  const form = new FormData();
  form.append("file", selectedFile);

  showOnly("progress");
  try {
    const res = await fetch(`${API}/api/upload`, { method: "POST", body: form });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    currentJobId = data.job_id;
    pollJob();
  } catch (err) {
    showError(err.message || String(err));
  }
});

async function pollJob() {
  try {
    const res = await fetch(`${API}/api/jobs/${currentJobId}`);
    if (!res.ok) throw new Error(await res.text());
    const job = await res.json();

    if (job.status === "error") {
      showError(job.error || "未知錯誤");
      return;
    }

    const [label, pct] = STATUS_LABEL[job.status] || ["處理緊…", 50];
    el("status-line").textContent = label;
    el("progress-fill").style.width = `${pct}%`;

    if (job.status === "awaiting_review") {
      currentHighlights = job.highlights;
      renderReview();
      return;
    }

    setTimeout(pollJob, 1500);
  } catch (err) {
    showError(err.message || String(err));
  }
}

function renderReview() {
  showOnly("review");
  const list = el("clip-list");
  list.innerHTML = "";
  currentHighlights.forEach((h) => {
    const item = document.createElement("div");
    item.className = "clip-item";
    item.innerHTML = `
      <input type="checkbox" data-index="${h.index}" ${h.selected ? "checked" : ""}>
      <div class="clip-info">
        <div class="clip-title">${escapeHtml(h.title || `片段 ${h.index + 1}`)}</div>
        <div class="clip-meta">${fmtTime(h.start)} - ${fmtTime(h.end)}</div>
        <div class="clip-reason">${escapeHtml(h.reason || "")}</div>
        <video controls preload="metadata" src="${API}/api/jobs/${currentJobId}/candidate/${h.index}"></video>
      </div>
    `;
    list.appendChild(item);
  });
}

function fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

el("render-btn").addEventListener("click", async () => {
  const checked = Array.from(el("clip-list").querySelectorAll('input[type="checkbox"]:checked'));
  const selectedIndices = checked.map((c) => parseInt(c.dataset.index, 10));
  if (selectedIndices.length === 0) {
    alert("請至少揀一段片段");
    return;
  }
  showOnly("render");
  el("render-status").textContent = "合併緊…";
  try {
    const res = await fetch(`${API}/api/jobs/${currentJobId}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_indices: selectedIndices }),
    });
    if (!res.ok) throw new Error(await res.text());
    pollRender();
  } catch (err) {
    showError(err.message || String(err));
  }
});

async function pollRender() {
  try {
    const res = await fetch(`${API}/api/jobs/${currentJobId}`);
    if (!res.ok) throw new Error(await res.text());
    const job = await res.json();

    if (job.status === "error") {
      showError(job.error || "未知錯誤");
      return;
    }

    if (job.status === "done" && job.has_final) {
      showOnly("done");
      const url = `${API}/api/jobs/${currentJobId}/download`;
      el("final-video").src = url;
      el("download-link").href = url;
      return;
    }

    setTimeout(pollRender, 1500);
  } catch (err) {
    showError(err.message || String(err));
  }
}

function showError(msg) {
  showOnly("error");
  el("error-msg").textContent = msg;
}

function resetToSetup() {
  currentJobId = null;
  currentHighlights = [];
  selectedFile = null;
  fileInput.value = "";
  fileLabel.textContent = "揀影片檔案 (或拖拽到呢度)";
  updateUploadEnabled();
  showOnly("setup");
}

el("restart-btn").addEventListener("click", resetToSetup);
el("error-restart-btn").addEventListener("click", resetToSetup);

showOnly("setup");
