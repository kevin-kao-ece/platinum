(function () {
  var STATUS_LABEL = {
    starting: "啟動中",
    running: "運行中",
    reloading: "重新載入中",
    stop: "已停止",
    error: "錯誤",
    unknown: "未知",
  };

  var setupSnapshot = null;
  /** @type {number | null} */
  var restartWatchTimer = null;
  /** @type {string | null} 最近一次成功載入／儲存後的表單快照（JSON） */
  var setupFormBaseline = null;
  /** 本次開啟 Setup 後曾成功「儲存設定」或「匯入 Tags」才可重新啟動 */
  var setupRestartAllowed = false;

  function $(id) {
    return document.getElementById(id);
  }

  function show(id, text, err) {
    var el = $(id);
    if (!el) return;
    el.textContent = text || "";
    var base = "msg";
    if (id === "setupMsg") base += " modal-msg";
    el.className = base + (err ? " err" : "");
  }

  function setBadge(el, runningStatus) {
    if (!el) return;
    var key = (runningStatus || "unknown").toLowerCase();
    var label = STATUS_LABEL[key] || runningStatus || "—";
    el.textContent = label;
    el.className =
      "badge badge-" +
      (key === "running"
        ? "running"
        : key === "reloading"
          ? "reloading"
          : key === "error"
            ? "error"
            : key === "starting"
              ? "starting"
              : "muted");
    el.title = "running_status: " + runningStatus;
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

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
    var clock =
      h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : pad2(m) + ":" + pad2(s);
    el.textContent = "試用剩餘 " + clock + "（" + rt + " 秒）";
    el.className = "remain-time mono" + (rt <= 300 ? " remain-low" : "");
    el.title = "remain_time: " + rt + " 秒";
  }

  function applyAppHead(data) {
    var name = data.app_name != null ? String(data.app_name).trim() : "";
    var verRaw = data.version;
    var ver =
      verRaw != null && String(verRaw).trim() !== ""
        ? String(verRaw).trim()
        : "";
    var h = $("appHeadTitle");
    if (name && h) {
      h.textContent = ver ? name + " V" + ver : name;
    } else if (h && !name) {
      h.textContent = "—";
    }
    if (name) {
      document.title = name + " 管理";
    }
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
      var data = await res.json().catch(function () {
        return {};
      });
      setBadge($("statusBadge"), data.running_status);
      setRemainTime(rte, data);
      applyAppHead(data);
      applyLicenseUI(data);
      if (!res.ok && rte) {
        rte.textContent = "無法取得狀態（HTTP " + res.status + "）";
        rte.className = "remain-time mono remain-ended";
        rte.title = res.statusText || "";
      }
      return data;
    } catch (e) {
      setBadge($("statusBadge"), "error");
      if (rte) {
        rte.textContent = "無法連線取得剩餘時間";
        rte.className = "remain-time mono remain-ended";
        rte.title = String(e);
      }
      return null;
    }
  }

  function stopRestartWatch() {
    if (restartWatchTimer != null) {
      clearTimeout(restartWatchTimer);
      restartWatchTimer = null;
    }
  }

  /**
   * 重載完成後清除 modal 訊息：曾出現 reloading 且已離開該狀態即視為完成。
   * 若極快完成而从未採到 reloading，則 5 秒後自動清除訊息；最長 90 秒後強制清除。
   */
  function startRestartWatch() {
    stopRestartWatch();
    var seenReloading = false;
    var tStart = Date.now();

    async function tick() {
      restartWatchTimer = null;
      var st = await fetchStatus();
      var rs = st
        ? String(st.running_status || "").toLowerCase()
        : "";
      if (rs === "reloading") {
        seenReloading = true;
      }
      var elapsed = Date.now() - tStart;
      var done =
        (seenReloading && rs !== "reloading") ||
        (!seenReloading && elapsed >= 5000) ||
        elapsed >= 90000;
      if (done) {
        show("setupMsg", "", false);
        await fetchStatus();
        return;
      }
      restartWatchTimer = window.setTimeout(tick, 400);
    }

    restartWatchTimer = window.setTimeout(tick, 400);
  }

  $("btnRefreshStatus").addEventListener("click", fetchStatus);

  function floatVal(id, fallback) {
    var el = $(id);
    if (!el) return fallback;
    var n = parseFloat(el.value);
    return isFinite(n) ? n : fallback;
  }

  function intVal(id, fallback) {
    var el = $(id);
    if (!el) return fallback;
    var n = parseInt(el.value, 10);
    return isFinite(n) ? n : fallback;
  }

  function strVal(id) {
    var el = $(id);
    return el ? String(el.value || "").trim() : "";
  }

  function fillPlcForm(prefix, plc) {
    if (!plc) return;
    var p = prefix + "_";
    var set = function (suffix, v) {
      var el = $(p + suffix);
      if (el && v != null) el.value = v;
    };
    set("ip", plc.ip != null ? plc.ip : "");
    set("port", plc.port != null ? plc.port : "");
    set("frame_type", plc.frame_type != null ? plc.frame_type : "");
    set("max_connections", plc.max_connections != null ? plc.max_connections : "");
    set("poll_interval", plc.poll_interval != null ? plc.poll_interval : "");
    set("connect_retries", plc.connect_retries != null ? plc.connect_retries : "");
    set(
      "connect_retry_delay_sec",
      plc.connect_retry_delay_sec != null ? plc.connect_retry_delay_sec : ""
    );
  }

  function fillInfluxMongo(data) {
    var inf = data.influxdb || {};
    var mongo = data.mongodb || {};
    var pairs = [
      ["influx_url", inf.url],
      ["influx_token", inf.token],
      ["influx_org", inf.org],
      ["influx_bucket", inf.bucket],
      ["influx_user", inf.user],
      ["influx_password", inf.password],
      ["influx_measurement", inf.measurement],
      ["mongo_host", mongo.host],
      ["mongo_port", mongo.port],
      ["mongo_database", mongo.database],
      ["mongo_collection", mongo.collection],
      ["mongo_user", mongo.user],
      ["mongo_password", mongo.password],
    ];
    pairs.forEach(function (pair) {
      var el = $(pair[0]);
      if (el && pair[1] != null) el.value = pair[1];
    });
  }

  function collectSetupPayload() {
    return {
      plc1: {
        ip: strVal("plc1_ip"),
        port: intVal("plc1_port", 6001),
        frame_type: strVal("plc1_frame_type") || "3E",
        max_connections: intVal("plc1_max_connections", 1),
        poll_interval: floatVal("plc1_poll_interval", 5),
        connect_retries: intVal("plc1_connect_retries", 3),
        connect_retry_delay_sec: floatVal("plc1_connect_retry_delay_sec", 0.5),
      },
      plc2: {
        ip: strVal("plc2_ip"),
        port: intVal("plc2_port", 6001),
        frame_type: strVal("plc2_frame_type") || "3E",
        max_connections: intVal("plc2_max_connections", 1),
        poll_interval: floatVal("plc2_poll_interval", 5),
        connect_retries: intVal("plc2_connect_retries", 3),
        connect_retry_delay_sec: floatVal("plc2_connect_retry_delay_sec", 0.5),
      },
      influxdb: {
        url: strVal("influx_url"),
        token: strVal("influx_token"),
        org: strVal("influx_org"),
        bucket: strVal("influx_bucket"),
        user: strVal("influx_user"),
        password: strVal("influx_password"),
        measurement: strVal("influx_measurement"),
      },
      mongodb: {
        host: strVal("mongo_host"),
        port: intVal("mongo_port", 27017),
        database: strVal("mongo_database"),
        collection: strVal("mongo_collection"),
        user: strVal("mongo_user"),
        password: strVal("mongo_password"),
      },
    };
  }

  function isSetupDirty() {
    if (setupFormBaseline === null) return false;
    try {
      return JSON.stringify(collectSetupPayload()) !== setupFormBaseline;
    } catch (e) {
      return true;
    }
  }

  function updateSetupActionButtons() {
    var saveBtn = $("btnSaveSetup");
    var restartBtn = $("btnRestart");
    if (!saveBtn || !restartBtn) return;
    var dirty = isSetupDirty();
    saveBtn.disabled = !dirty;
    restartBtn.disabled = !(setupRestartAllowed && !dirty);
  }

  function escapeCsvCell(s) {
    var t = String(s == null ? "" : s);
    if (/[",\r\n]/.test(t)) return '"' + t.replace(/"/g, '""') + '"';
    return t;
  }

  function tagsToCsv(tags) {
    var lines = ["tag_name,access,device,datatype"];
    if (!tags || typeof tags !== "object") return lines.join("\r\n");
    Object.keys(tags).forEach(function (name) {
      var t = tags[name] || {};
      lines.push(
        [
          escapeCsvCell(name),
          escapeCsvCell(t.access),
          escapeCsvCell(t.device),
          escapeCsvCell(t.datatype),
        ].join(",")
      );
    });
    return lines.join("\r\n");
  }

  function downloadText(filename, text, mime) {
    var blob = new Blob([text], { type: mime || "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }

  function exportTagsForPlc(index) {
    var snap = setupSnapshot;
    if (!snap) {
      show("setupMsg", "請稍候再試（尚未載入設定）", true);
      return;
    }
    var key = index === 0 ? "plc1" : "plc2";
    var plc = snap[key];
    var tags = plc && plc.tags ? plc.tags : {};
    if (!tags || !Object.keys(tags).length) {
      show("setupMsg", "目前無 Tags 可匯出", true);
      return;
    }
    var csv = tagsToCsv(tags);
    var base = index === 0 ? "plc1_tags" : "plc2_tags";
    downloadText(base + ".csv", csv, "text/csv;charset=utf-8");
    show("setupMsg", "已下載 " + base + ".csv", false);
  }

  async function loadSetupIntoForms() {
    show("setupMsg", "載入中…", false);
    var saveBtn = $("btnSaveSetup");
    var restartBtn = $("btnRestart");
    if (saveBtn) saveBtn.disabled = true;
    if (restartBtn) restartBtn.disabled = true;
    try {
      var res = await fetch("/api/config/setup", { cache: "no-store" });
      var data = await res.json().catch(function () {
        return null;
      });
      if (!res.ok || !data) {
        show(
          "setupMsg",
          (data && data.detail) || "無法載入設定（HTTP " + res.status + "）",
          true
        );
        updateSetupActionButtons();
        return;
      }
      setupSnapshot = data;
      fillPlcForm("plc1", data.plc1);
      fillPlcForm("plc2", data.plc2);
      fillInfluxMongo(data);
      show("setupMsg", "", false);
      setupFormBaseline = JSON.stringify(collectSetupPayload());
      updateSetupActionButtons();
    } catch (e) {
      show("setupMsg", String(e), true);
      updateSetupActionButtons();
    }
  }

  function openSetupModal() {
    var modal = $("setupModal");
    if (!modal) return;
    setupRestartAllowed = false;
    setupFormBaseline = null;
    activateSetupTab("plc1");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    loadSetupIntoForms();
  }

  function closeSetupModal() {
    var modal = $("setupModal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  function activateSetupTab(tabId) {
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      var on = btn.getAttribute("data-tab") === tabId;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach(function (panel) {
      var on = panel.getAttribute("data-panel") === tabId;
      panel.classList.toggle("active", on);
    });
  }

  var btnOpen = $("btnOpenSetup");
  if (btnOpen) btnOpen.addEventListener("click", openSetupModal);
  var btnClose = $("btnCloseSetup");
  if (btnClose) btnClose.addEventListener("click", closeSetupModal);

  $("setupModal").addEventListener("click", function (e) {
    if (e.target === $("setupModal")) closeSetupModal();
  });

  var setupModalBody = document.querySelector("#setupModal .modal-body");
  if (setupModalBody) {
    setupModalBody.addEventListener("input", function () {
      updateSetupActionButtons();
    });
    setupModalBody.addEventListener("change", function () {
      updateSetupActionButtons();
    });
  }

  document.querySelectorAll(".tab-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tab = btn.getAttribute("data-tab");
      if (tab) activateSetupTab(tab);
    });
  });

  $("btnSaveSetup").addEventListener("click", async function () {
    show("setupMsg", "儲存中…", false);
    try {
      var res = await fetch("/api/config/setup", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectSetupPayload()),
      });
      var data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        var detail = data.detail;
        show(
          "setupMsg",
          typeof detail === "string"
            ? detail
            : JSON.stringify(detail || data),
          true
        );
        updateSetupActionButtons();
        return;
      }
      show("setupMsg", data.message || "已儲存", false);
      setupRestartAllowed = true;
      await loadSetupIntoForms();
    } catch (e) {
      show("setupMsg", String(e), true);
      updateSetupActionButtons();
    }
  });

  $("btnRestart").addEventListener("click", async function () {
    if (
      !confirm(
        "確定重新啟動？將短暫停止 PLC 輪詢並重讀設定。"
      )
    )
      return;
    stopRestartWatch();
    show("setupMsg", "送出中…", false);
    try {
      var res = await fetch("/api/system/restart", { method: "POST" });
      var data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        show(
          "setupMsg",
          (typeof data.detail === "string"
            ? data.detail
            : data.message) ||
            res.statusText ||
            "",
          true
        );
        await fetchStatus();
        return;
      }
      show("setupMsg", "重新載入中…", false);
      await fetchStatus();
      startRestartWatch();
    } catch (e) {
      show("setupMsg", String(e), true);
    }
  });

  $("btnExportTagsPlc1").addEventListener("click", function () {
    exportTagsForPlc(0);
  });
  $("btnExportTagsPlc2").addEventListener("click", function () {
    exportTagsForPlc(1);
  });

  function bindTagsImport(fileInputId, fileNameId, btnId, plcIndex) {
    $(fileInputId).addEventListener("change", function () {
      var f = this.files && this.files[0];
      $(fileNameId).textContent = f ? f.name : "未選擇檔案";
    });
    $(btnId).addEventListener("click", async function () {
      var input = $(fileInputId);
      var f = input.files && input.files[0];
      if (!f) {
        show("setupMsg", "請先選擇 CSV", true);
        return;
      }
      show("setupMsg", "上傳中…", false);
      var fd = new FormData();
      fd.append("file", f, f.name);
      try {
        var res = await fetch("/api/config/plc/" + plcIndex + "/tags/import", {
          method: "POST",
          body: fd,
        });
        var data = await res.json().catch(function () {
          return {};
        });
        if (!res.ok) {
          show(
            "setupMsg",
            typeof data.detail === "string"
              ? data.detail
              : JSON.stringify(data.detail || data),
            true
          );
          updateSetupActionButtons();
          return;
        }
        show("setupMsg", data.message || "匯入完成", false);
        input.value = "";
        $(fileNameId).textContent = "未選擇檔案";
        setupRestartAllowed = true;
        await loadSetupIntoForms();
      } catch (e) {
        show("setupMsg", String(e), true);
        updateSetupActionButtons();
      }
    });
  }

  bindTagsImport("plc1TagsFile", "plc1TagsFileName", "btnImportTagsPlc1", 0);
  bindTagsImport("plc2TagsFile", "plc2TagsFileName", "btnImportTagsPlc2", 1);

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
      var data = await res.json().catch(function () {
        return {};
      });
      show(
        "licMsg",
        data.message || JSON.stringify(data, null, 2),
        data.status === "error"
      );
      fetchStatus();
    } catch (e) {
      show("licMsg", String(e), true);
    }
  });

  var logOpen = false;
  async function loadLogs() {
    var pre = $("logViewer");
    var countEl = $("logLineCount");
    try {
      var res = await fetch("/api/logs/tail?lines=150", { cache: "no-store" });
      var data = await res.json().catch(function () {
        return {};
      });
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
