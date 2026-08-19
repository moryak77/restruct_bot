"use strict";

/* ===================== утилиты ===================== */

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function api() {
  return window.pywebview && window.pywebview.api;
}

function showToast(message, isError = false) {
  const container = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 260);
  }, 2600);
}

function animateNumber(el, target, duration = 500) {
  const start = parseInt(el.textContent.replace(/\D/g, ""), 10) || 0;
  const t0 = performance.now();
  function tick(now) {
    const p = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    const value = Math.round(start + (target - start) * eased);
    el.textContent = value.toLocaleString("ru-RU");
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function copyToClipboard(text, statusEl) {
  navigator.clipboard.writeText(text).then(() => {
    showToast("Отчёт скопирован в буфер обмена.");
    if (statusEl) statusEl.textContent = "Отчёт скопирован в буфер обмена.";
  }).catch(() => showToast("Не удалось скопировать.", true));
}

/* ===================== навигация ===================== */

const nav = document.getElementById("nav");
const navIndicator = document.getElementById("navIndicator");
const navItems = Array.from(document.querySelectorAll(".nav-item"));
const views = Array.from(document.querySelectorAll(".view"));

function moveIndicator(target) {
  navIndicator.style.transform = `translateY(${target.offsetTop}px)`;
}

function switchView(key) {
  navItems.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === key));
  views.forEach((v) => v.classList.toggle("active", v.id === `view-${key}`));
  const activeBtn = navItems.find((b) => b.dataset.view === key);
  if (activeBtn) moveIndicator(activeBtn);

  if (key === "processes" && !state.processesLoaded) loadProcesses();
  if (key === "recycle") loadRecycleBin();
  if (key === "activity") loadActivity();
  if (key === "logs") loadLogs();
}

navItems.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
window.addEventListener("load", () => moveIndicator(navItems[0]));
window.addEventListener("resize", () => {
  const active = navItems.find((b) => b.classList.contains("active"));
  if (active) moveIndicator(active);
});

/* ===================== общее состояние ===================== */

const state = {
  processesLoaded: false,
  allProcesses: [],
  selectedPid: null,
};

/* ===================== ПРОВЕРКА ===================== */

const scanBtn = document.getElementById("scanBtn");
const copyScanBtn = document.getElementById("copyScanBtn");
const scanStatus = document.getElementById("scanStatus");
const scanProgress = document.getElementById("scanProgress");
const scanConsole = document.getElementById("scanConsole");
let lastScanReport = "";

function consoleLine(container, text, cls = "") {
  const div = document.createElement("div");
  div.className = "console-line " + cls;
  div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function streamLines(container, lines, delay = 18) {
  return new Promise((resolve) => {
    let i = 0;
    function next() {
      if (i >= lines.length) return resolve();
      const [cls, text] = lines[i++];
      consoleLine(container, text, cls);
      setTimeout(next, text.trim() ? delay : 8);
    }
    next();
  });
}

function classifyReportLine(line) {
  if (line.startsWith("✗") || line.toUpperCase().includes("НАЙДЕНЫ ПОДОЗРИТЕЛЬНЫЕ") || line.toUpperCase().includes("ПОДОЗРИТЕЛЬНЫЕ МОДУЛИ")) return "flag";
  if (line.startsWith("✓")) return "ok";
  if (line.startsWith("⚠") || line.startsWith("ℹ")) return "warn";
  if (line.startsWith("RESTRUCT") || line.startsWith("---")) return "title";
  return "";
}

async function runScan() {
  scanBtn.disabled = true;
  copyScanBtn.disabled = true;
  scanStatus.textContent = "Сканирование...";
  scanProgress.classList.add("active");
  scanProgress.style.width = "15%";
  scanConsole.innerHTML = "";

  let dots = 0;
  const spinner = setInterval(() => {
    dots = (dots + 1) % 4;
    scanStatus.textContent = "Сканирование" + ".".repeat(dots);
  }, 320);

  try {
    const result = await api().run_scan();
    clearInterval(spinner);
    scanProgress.style.width = "100%";

    animateNumber(document.getElementById("statProcesses"), result.all_processes.length);
    animateNumber(document.getElementById("statFlags"), result.flagged_processes.length);
    document.getElementById("statGame").textContent = result.game_process_found ? "найден" : "не запущен";
    animateNumber(document.getElementById("statModules"), result.module_findings.length);
    document.getElementById("statFlags").style.color = result.flagged_processes.length ? "var(--danger)" : "var(--ok)";

    const lines = result.reportText.split("\n").map((l) => [classifyReportLine(l), l]);
    await streamLines(scanConsole, lines);

    lastScanReport = result.reportText;
    copyScanBtn.disabled = false;
    scanStatus.textContent = result.flagged_processes.length
      ? `Найдено совпадений: ${result.flagged_processes.length}.`
      : "Совпадений не найдено.";
    scanStatus.style.color = result.flagged_processes.length ? "var(--danger)" : "var(--ok)";
  } catch (e) {
    clearInterval(spinner);
    consoleLine(scanConsole, "Ошибка: " + e, "flag");
    scanStatus.textContent = "Ошибка сканирования.";
  } finally {
    setTimeout(() => scanProgress.classList.remove("active"), 400);
    scanBtn.disabled = false;
    scanBtn.innerHTML = '<span class="btn-icon">▶</span>Повторить проверку';
  }
}

scanBtn.addEventListener("click", runScan);
copyScanBtn.addEventListener("click", () => copyToClipboard(lastScanReport, scanStatus));

/* ===================== ВСЕ ПРОЦЕССЫ ===================== */

const processTableBody = document.getElementById("processTableBody");
const processSearch = document.getElementById("processSearch");
const processCount = document.getElementById("processCount");
const processDetailGrid = document.getElementById("processDetailGrid");

async function loadProcesses() {
  processTableBody.innerHTML = `<tr><td colspan="4" class="detail-empty">Загрузка...</td></tr>`;
  const list = await api().get_processes();
  state.allProcesses = list;
  state.processesLoaded = true;
  renderProcessTable();
}

function renderProcessTable() {
  const q = processSearch.value.trim().toLowerCase();
  const filtered = q ? state.allProcesses.filter((p) => p.name.toLowerCase().includes(q)) : state.allProcesses;
  processCount.textContent = `Показано ${filtered.length} из ${state.allProcesses.length}`;

  processTableBody.innerHTML = filtered.map((p) => `
    <tr class="clickable ${p.flagged ? "row-flagged" : ""} ${p.pid === state.selectedPid ? "selected" : ""}" data-pid="${p.pid}">
      <td>${p.pid}</td>
      <td>${escapeHtml(p.name)}</td>
      <td title="${escapeHtml(p.exe)}">${escapeHtml(p.exe)}</td>
      <td>${p.flagged ? "⚠ совпадение" : "ок"}</td>
    </tr>
  `).join("");

  processTableBody.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => selectProcess(parseInt(row.dataset.pid, 10)));
  });
}

async function selectProcess(pid) {
  state.selectedPid = pid;
  renderProcessTable();
  const proc = state.allProcesses.find((p) => p.pid === pid);
  if (!proc) return;

  const detail = await api().get_process_detail(pid);
  processDetailGrid.innerHTML = `
    <div class="k">Имя</div><div class="v">${escapeHtml(proc.name)}</div>
    <div class="k">PID</div><div class="v">${proc.pid}</div>
    <div class="k">Родительский процесс</div><div class="v">${detail.ok ? escapeHtml(detail.parent) : "—"}</div>
    <div class="k">Запущен</div><div class="v">${detail.ok ? escapeHtml(detail.started) : "—"}</div>
    <div class="k">Путь</div><div class="v">${escapeHtml(proc.exe)}</div>
    <div class="detail-actions">
      <button class="btn btn-ghost" id="signBtn">🔏 Проверить цифровую подпись</button>
      <span class="sign-result" id="signResult"></span>
    </div>
  `;
  document.getElementById("signBtn").addEventListener("click", async () => {
    const btn = document.getElementById("signBtn");
    const resultEl = document.getElementById("signResult");
    btn.disabled = true;
    btn.textContent = "Проверяю...";
    const status = await api().check_signature(proc.exe);
    btn.disabled = false;
    btn.innerHTML = "🔏 Проверить цифровую подпись";
    if (status === "signed") {
      resultEl.textContent = "✓ Есть цифровая подпись";
      resultEl.className = "sign-result ok";
    } else if (status === "unsigned") {
      resultEl.textContent = "⚠ Нет встроенной подписи (часть системных файлов Windows подписана через каталоги, а не встроенно)";
      resultEl.className = "sign-result warn";
    } else {
      resultEl.textContent = "Не удалось проверить.";
      resultEl.className = "sign-result";
    }
  });
}

processSearch.addEventListener("input", renderProcessTable);
document.getElementById("refreshProcessesBtn").addEventListener("click", loadProcesses);

/* ===================== СКАН ДИСКА ===================== */

const diskScanBtn = document.getElementById("diskScanBtn");
const copyDiskBtn = document.getElementById("copyDiskBtn");
const diskStatus = document.getElementById("diskStatus");
const diskProgress = document.getElementById("diskProgress");
const diskConsole = document.getElementById("diskConsole");
let diskScanning = false;
let diskStartTime = 0;
let diskTimerInterval = null;
let diskLastReport = "";

async function toggleDiskScan() {
  if (diskScanning) {
    await api().stop_disk_scan();
    diskScanBtn.disabled = true;
    diskScanBtn.textContent = "Останавливаю...";
    return;
  }
  diskScanning = true;
  diskStartTime = Date.now();
  diskConsole.innerHTML = "";
  copyDiskBtn.disabled = true;
  diskScanBtn.innerHTML = '<span class="btn-icon">⏹</span>Остановить';
  diskProgress.classList.add("active");
  diskStatus.textContent = "Сканирование запущено...";
  document.getElementById("diskScanned").textContent = "0";
  document.getElementById("diskMatches").textContent = "0";

  diskTimerInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - diskStartTime) / 1000);
    document.getElementById("diskTime").textContent = `${secs} сек`;
  }, 500);

  await api().start_disk_scan();
}

window.onDiskProgress = function (count) {
  animateNumber(document.getElementById("diskScanned"), count, 250);
  diskStatus.textContent = `Сканирование... просмотрено ${count.toLocaleString("ru-RU")} файлов`;
};

window.onDiskMatch = function (finding) {
  const current = parseInt(document.getElementById("diskMatches").textContent.replace(/\D/g, ""), 10) || 0;
  document.getElementById("diskMatches").textContent = current + 1;
  document.getElementById("diskMatches").style.color = "var(--danger)";
  consoleLine(diskConsole, "✗ " + finding.path, "flag");
  consoleLine(diskConsole, "   совпадение: " + finding.matched_rule, "muted");
};

window.onDiskDone = function (scanned, reportText) {
  diskScanning = false;
  clearInterval(diskTimerInterval);
  diskProgress.classList.remove("active");
  diskLastReport = reportText;
  const matches = parseInt(document.getElementById("diskMatches").textContent.replace(/\D/g, ""), 10) || 0;
  animateNumber(document.getElementById("diskScanned"), scanned, 300);

  if (matches) {
    diskStatus.textContent = `Готово. Найдено совпадений: ${matches}.`;
    diskStatus.style.color = "var(--danger)";
  } else {
    diskStatus.textContent = `Готово. Просканировано ${scanned.toLocaleString("ru-RU")} файлов, совпадений не найдено.`;
    diskStatus.style.color = "var(--ok)";
    consoleLine(diskConsole, "✓ Совпадений не найдено.", "ok");
  }

  diskScanBtn.disabled = false;
  diskScanBtn.innerHTML = '<span class="btn-icon">▶</span>Начать скан диска';
  copyDiskBtn.disabled = false;
};

diskScanBtn.addEventListener("click", toggleDiskScan);
copyDiskBtn.addEventListener("click", () => copyToClipboard(diskLastReport, diskStatus));

/* ===================== КОРЗИНА ===================== */

const recycleTableBody = document.getElementById("recycleTableBody");
const recycleCount = document.getElementById("recycleCount");

function fmtSize(bytes) {
  if (!bytes) return "—";
  return Math.round(bytes / 1024).toLocaleString("ru-RU") + " КБ";
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function loadRecycleBin() {
  recycleTableBody.innerHTML = `<tr><td colspan="4" class="detail-empty">Загрузка...</td></tr>`;
  const data = await api().get_recycle_bin();
  const items = data.items;
  const now = Date.now();
  let flaggedCount = 0;

  recycleTableBody.innerHTML = items.map((item) => {
    const deletedMs = item.deleted_at ? new Date(item.deleted_at).getTime() : null;
    const isRecent = deletedMs && (now - deletedMs) < 3600 * 1000;
    let cls = "", status = "—";
    if (item.flagged) { cls = "row-flagged"; status = "⚠ совпадение"; flaggedCount++; }
    else if (isRecent) { cls = "row-recent"; status = "недавно (<1 ч)"; }
    return `<tr class="${cls}">
      <td>${fmtDate(item.deleted_at)}</td>
      <td title="${escapeHtml(item.original_path)}">${escapeHtml(item.original_path)}</td>
      <td>${fmtSize(item.size)}</td>
      <td>${status}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="4" class="detail-empty">Корзина пуста.</td></tr>`;

  recycleCount.textContent = `Всего в корзине: ${items.length}` + (flaggedCount ? ` · совпадений: ${flaggedCount}` : "");
}

document.getElementById("refreshRecycleBtn").addEventListener("click", loadRecycleBin);

/* ===================== АКТИВНОСТЬ ===================== */

const prefetchTableBody = document.getElementById("prefetchTableBody");
const mruTableBody = document.getElementById("mruTableBody");

async function loadActivity() {
  prefetchTableBody.innerHTML = `<tr><td colspan="3" class="detail-empty">Загрузка...</td></tr>`;
  mruTableBody.innerHTML = `<tr><td colspan="3" class="detail-empty">Загрузка...</td></tr>`;

  const cfg = await api().get_config();
  const everythingText = {
    installed: "Everything найден на компьютере (не запущен).",
    running: "Everything сейчас запущен.",
    not_found: "Everything не найден — не критично, «Скан диска» и так проверяет все файлы.",
  }[cfg.everything] || "";
  document.getElementById("everythingStatus").textContent = everythingText;

  const data = await api().get_activity();

  if (data.prefetchError) {
    prefetchTableBody.innerHTML = `<tr><td colspan="3" class="detail-empty">${escapeHtml(data.prefetchError)}</td></tr>`;
  } else if (!data.prefetch.length) {
    prefetchTableBody.innerHTML = `<tr><td colspan="3" class="detail-empty">Нет данных Prefetch на этом компьютере.</td></tr>`;
  } else {
    prefetchTableBody.innerHTML = data.prefetch.map((e) => `
      <tr class="${e.flagged ? "row-flagged" : ""}">
        <td>${escapeHtml(e.exe_name)}</td>
        <td>${fmtDate(e.last_run)}</td>
        <td>${e.flagged ? "⚠ совпадение" : "—"}</td>
      </tr>`).join("");
  }

  if (!data.mru.length) {
    mruTableBody.innerHTML = `<tr><td colspan="3" class="detail-empty">Нет данных или ключи реестра пусты.</td></tr>`;
  } else {
    mruTableBody.innerHTML = data.mru.map((m) => `
      <tr class="${m.flagged ? "row-flagged" : ""}">
        <td>${escapeHtml(m.source)}</td>
        <td>${escapeHtml(m.value)}</td>
        <td>${m.flagged ? "⚠ совпадение" : "—"}</td>
      </tr>`).join("");
  }
}

document.getElementById("refreshActivityBtn").addEventListener("click", loadActivity);

/* ===================== ЛОГИ ===================== */

const logsList = document.getElementById("logsList");
const logViewer = document.getElementById("logViewer");

async function loadLogs() {
  const cfg = await api().get_config();
  document.getElementById("logsDirHint").textContent = `Каждая проверка сохраняется файлом в ${cfg.logDir} — ничего не отправляется, только локально.`;

  const logs = await api().get_logs();
  if (!logs.length) {
    logsList.innerHTML = `<div class="detail-empty">Логов пока нет.</div>`;
    return;
  }
  logsList.innerHTML = logs.map((l) => `
    <div class="log-item" data-name="${escapeHtml(l.name)}">
      <div class="name">${escapeHtml(l.stem)}</div>
      <div class="time">${escapeHtml(l.mtime)}</div>
    </div>
  `).join("");
  logsList.querySelectorAll(".log-item").forEach((el) => {
    el.addEventListener("click", async () => {
      logsList.querySelectorAll(".log-item").forEach((i) => i.classList.remove("active"));
      el.classList.add("active");
      const content = await api().read_log(el.dataset.name);
      logViewer.innerHTML = "";
      consoleLine(logViewer, content, "");
    });
  });
}

/* ===================== старт ===================== */

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.activeElement && document.activeElement.blur();
});
