(function () {
  var STATUS_LABEL = {
    starting: "啟動中",
    running: "運行中",
    reloading: "重新載入中",
    stop: "已停止",
    error: "錯誤",
    unknown: "未知",
  };

  function $(id) {
    return document.getElementById(id);
  }

  function show(id, text, err) {
    var el = $(id);
    if (!el) return;
    el.textContent = text || "";
    el.className = "msg" + (err ? " err" : "");
  }

  function setBadge(el, runningStatus) {
    if (!el) return;
    var key = (runningStatus || "unknown").toLowerCase();
    var label = STATUS_LABEL[key] || runningStatus || "—";
    el.textContent = label;
    el.className = "badge badge-" + (key === "running" ? "running" : key === "reloading" ? "reloading" : key === "error" ? "error" : key === "starting" ? "starting" : "muted");
    el.title = "running_status: " + runningStatus;
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /**
   * remain_time：試用為剩餘秒數；已結束為 0；正式授權時後端仍可能傳 -1，但以 license_status 為準。
   */
  function setRemainTime(el, data) {
    if (!el) return;
    var licensed = data.license_status === "Licensed";
    if (licensed) {
      el.textContent = "無限制（正式授權）";
      el.className = "remain-time mono remain-licensed";
      el.title = "license_status: Licensed";
      return;
    }
    var raw = data.remain_time;
    var rt = raw != null && raw !== "" ? Number(raw) : NaN;
    if (!isFinite(rt)) {
      el.textContent = "試用剩餘時間載入中…";
      el.className = "remain-time mono";
      el.title = "remain_time 尚未取得";
      return;
    }
    if (rt < 0) {
      el.textContent = "授權狀態與憑證不一致（請確認 license.crt）";
      el.className = "remain-time mono remain-ended";
      el.title = "remain_time: " + rt;
      return;
    }
    if (rt <= 0) {
      el.textContent = "試用時間已結束";
      el.className = "remain-time mono remain-ended";
      el.title = "remain_time: 0（秒）";
      return;
    }
    var h = Math.floor(rt / 3600);
    var m = Math.floor((rt % 3600) / 60);
    var s = Math.floor(rt % 60);
    var clock = (h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : pad2(m) + ":" + pad2(s));
    el.textContent = "試用剩餘 " + clock + "（" + rt + " 秒）";
    el.className =
      "remain-time mono" + (rt <= 300 ? " remain-low" : "");
    el.title = "remain_time: " + rt + " 秒";
  }

  function applyLicenseUI(data) {
    var noPanel = $("licenseStepsPanel");
    var okPanel = $("licenseOkPanel");
    var contact = $("licenseContactEl");
    if (!noPanel || !okPanel) return;
    if (contact && data.license_contact) {
      contact.textContent = data.license_contact;
    }
    var noLic = data.license_status === "No License";
    noPanel.classList.toggle("hidden", !noLic);
    okPanel.classList.toggle("hidden", noLic);
  }

  async function fetchStatus() {
    var rte = $("remainTimeEl");
    try {
      var res = await fetch("/api/status", { cache: "no-store" });
      var data = await res.json().catch(function () { return {}; });
      setBadge($("statusBadge"), data.running_status);
      setRemainTime(rte, data);
      applyLicenseUI(data);
      if (!res.ok && rte) {
        rte.textContent = "無法取得狀態（HTTP " + res.status + "）";
        rte.className = "remain-time mono remain-ended";
        rte.title = res.statusText || "";
      }
    } catch (e) {
      setBadge($("statusBadge"), "error");
      if (rte) {
        rte.textContent = "無法連線取得剩餘時間";
        rte.className = "remain-time mono remain-ended";
        rte.title = String(e);
      }
    }
  }

  $("btnRefreshStatus").addEventListener("click", fetchStatus);

  $("cfgFile").addEventListener("change", function () {
    var f = this.files && this.files[0];
    $("cfgFileName").textContent = f ? f.name : "未選擇檔案";
  });

  $("btnImportCfg").addEventListener("click", async function () {
    var input = $("cfgFile");
    var f = input.files && input.files[0];
    if (!f) {
      show("cfgMsg", "請先選擇 YAML 檔", true);
      return;
    }
    show("cfgMsg", "上傳中…");
    var fd = new FormData();
    fd.append("file", f, f.name);
    try {
      var res = await fetch("/api/config/import", { method: "POST", body: fd });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        show("cfgMsg", data.detail || JSON.stringify(data), true);
        return;
      }
      show("cfgMsg", data.message || "完成");
      fetchStatus();
    } catch (e) {
      show("cfgMsg", String(e), true);
    }
  });

  $("licFile").addEventListener("change", function () {
    var f = this.files && this.files[0];
    $("licFileName").textContent = f ? f.name : "未選擇檔案";
  });

  $("btnLicSubmit").addEventListener("click", async function () {
    var input = $("licFile");
    var f = input.files && input.files[0];
    if (!f) {
      show("licMsg", "請選擇憑證檔", true);
      return;
    }
    show("licMsg", "上傳中…");
    var fd = new FormData();
    fd.append("file", f, f.name);
    try {
      var res = await fetch("/verify_license", { method: "POST", body: fd });
      var data = await res.json().catch(function () { return {}; });
      show("licMsg", data.message || JSON.stringify(data, null, 2), data.status === "error");
      fetchStatus();
    } catch (e) {
      show("licMsg", String(e), true);
    }
  });

  $("btnRestart").addEventListener("click", async function () {
    if (!confirm("確定重新啟動？將短暫停止 PLC 輪詢並重讀設定（Web 不斷線）。")) return;
    show("restartMsg", "送出中…");
    try {
      var res = await fetch("/api/system/restart", { method: "POST" });
      var data = await res.json().catch(function () { return {}; });
      show("restartMsg", data.message || data.status || res.statusText, !res.ok);
      fetchStatus();
    } catch (e) {
      show("restartMsg", String(e), true);
    }
  });

  var logOpen = false;
  async function loadLogs() {
    var pre = $("logViewer");
    var countEl = $("logLineCount");
    try {
      var res = await fetch("/api/logs/tail?lines=150", { cache: "no-store" });
      var data = await res.json().catch(function () { return {}; });
      var lines = data.logs || [];
      if (countEl) countEl.textContent = String(lines.length);
      if (pre) pre.textContent = lines.join("\n");
    } catch (e) {
      if (pre) pre.textContent = String(e);
    }
  }

  $("btnToggleLogs").addEventListener("click", async function () {
    logOpen = !logOpen;
    var panel = $("logPanel");
    if (!panel) return;
    panel.classList.toggle("hidden", !logOpen);
    this.textContent = logOpen ? "隱藏最近日誌" : "檢視最近日誌";
    if (logOpen) await loadLogs();
  });

  $("btnRefreshLogs").addEventListener("click", loadLogs);

  fetchStatus();
  setInterval(fetchStatus, 8000);
})();
