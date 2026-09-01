(function () {
  const API_BASE = window.API_BASE_URL;
  const POLL_INTERVAL_MS = 4000;

  const screens = {
    home: document.getElementById("screen-home"),
    select: document.getElementById("screen-select"),
    confirm: document.getElementById("screen-confirm"),
    processing: document.getElementById("screen-processing"),
    results: document.getElementById("screen-results"),
    error: document.getElementById("screen-error"),
    refcheckConfirm: document.getElementById("screen-refcheck-confirm"),
    refcheck: document.getElementById("screen-refcheck"),
  };

  let contractsById = {};
  let currentMode = null; // "validate" | "refcheck"
  let selectedContract = null;
  let currentJobId = null;
  let pollTimer = null;
  let allCodes = [];
  let codesFilter = "all";

  let currentRefJobId = null;
  let refStatusPollTimer = null;
  let refLogPollTimer = null;

  function showScreen(name) {
    Object.values(screens).forEach((s) => s.classList.add("hidden"));
    screens[name].classList.remove("hidden");
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    stopRefPolling();
  }

  function stopRefPolling() {
    if (refStatusPollTimer) {
      clearTimeout(refStatusPollTimer);
      refStatusPollTimer = null;
    }
    if (refLogPollTimer) {
      clearTimeout(refLogPollTimer);
      refLogPollTimer = null;
    }
  }

  async function loadContracts() {
    try {
      const res = await fetch(`${API_BASE}/api/contracts`);
      if (!res.ok) throw new Error("bad response");
      const data = await res.json();
      contractsById = {};
      data.contracts.forEach((c) => {
        contractsById[c.contract] = c;
      });
    } catch (err) {
      contractsById = null; // signals "could not reach server" to renderContractGrid
    }
  }

  function renderContractGrid() {
    const grid = document.getElementById("contract-grid");
    const lead = document.getElementById("select-lead");
    lead.textContent =
      currentMode === "refcheck"
        ? "Select a Contract ID to run the Reference Integrity Test."
        : "Select a Contract ID to start processing.";

    if (!contractsById) {
      grid.innerHTML = `<p class="muted">Could not reach the processing server. Please check back later.</p>`;
      return;
    }
    grid.innerHTML = "";
    Object.values(contractsById).forEach((c) => {
      const card = document.createElement("div");
      card.className = "contract-card";
      card.innerHTML = `
        <div class="contract-id">${c.contract}</div>
        <div class="contract-org">${c.org}</div>
        <div class="select-label">Select</div>
      `;
      card.addEventListener("click", () => selectContract(c.contract));
      grid.appendChild(card);
    });
  }

  function openMode(mode) {
    currentMode = mode;
    renderContractGrid();
    showScreen("select");
  }

  function selectContract(contract) {
    selectedContract = contract;
    const c = contractsById[contract] || {};

    if (currentMode === "refcheck") {
      document.getElementById("refconfirm-contract").textContent = contract;
      document.getElementById("refconfirm-org").textContent = c.org || "-";
      document.getElementById("refconfirm-contract-id").textContent = c.contract || contract;
      document.getElementById("refconfirm-url").textContent = c.index_url || "-";
      showScreen("refcheckConfirm");
      return;
    }

    document.getElementById("confirm-contract").textContent = contract;
    document.getElementById("confirm-org").textContent = c.org || "-";
    document.getElementById("confirm-contract-id").textContent = c.contract || contract;
    document.getElementById("confirm-url").textContent = c.index_url || "-";
    showScreen("confirm");
  }

  async function startProcessing() {
    stopPolling();
    document.getElementById("processing-contract").textContent = selectedContract;
    showScreen("processing");

    try {
      const res = await fetch(`${API_BASE}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contract: selectedContract }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || "Could not start processing.");
      }
      const data = await res.json();
      currentJobId = data.job_id;
      pollJob();
    } catch (err) {
      showError(err.message || "Could not start processing.");
    }
  }

  async function pollJob() {
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${currentJobId}`);
      if (!res.ok) throw new Error("Lost contact with the processing server.");
      const job = await res.json();

      if (job.status === "running") {
        pollTimer = setTimeout(pollJob, POLL_INTERVAL_MS);
        return;
      }
      if (job.status === "completed") {
        showResults(job);
        return;
      }
      // failed
      showError(job.error || "Processing failed. Please try again.");
    } catch (err) {
      showError(networkErrorMessage(err));
    }
  }

  function showResults(job) {
    document.getElementById("results-contract").textContent = job.contract;

    const s = job.summary || {};
    const rows = [
      ["Total Records Processed", fmt(s.total_records_processed)],
      ["Valid Records", fmt(s.valid_records)],
      ["Failed Records", fmt(s.failed_records)],
      ["Checks Passed", fmt(s.checks_passed)],
      ["Checks Failed", fmt(s.checks_failed)],
      ["Error Codes Exercised", `${fmt(s.codes_tested)} of ${fmt(s.codes_total)}`],
      ["CSV Files Generated", fmt(s.csv_files_generated)],
    ];
    const tbody = document.getElementById("summary-table-body");
    tbody.innerHTML = rows
      .map(([label, val]) => `<tr><td>${label}</td><td>${val}</td></tr>`)
      .join("");

    const list = document.getElementById("file-list");
    list.innerHTML = "";
    (job.files || []).forEach((filename) => {
      const li = document.createElement("li");
      const link = `${API_BASE}/api/jobs/${job.job_id}/files/${encodeURIComponent(filename)}`;
      li.innerHTML = `
        <span class="file-name">${filename}</span>
        <a class="btn btn-primary" href="${link}" target="_blank" rel="noopener">Download</a>
      `;
      list.appendChild(li);
    });

    showScreen("results");
    loadCodes(job.job_id);
  }

  async function loadCodes(jobId) {
    const tbody = document.getElementById("codes-table-body");
    const subtitle = document.getElementById("codes-subtitle");
    tbody.innerHTML = `<tr><td colspan="7" class="muted">Loading error code detail...</td></tr>`;
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}/codes`);
      if (!res.ok) throw new Error("could not load code detail");
      const data = await res.json();
      allCodes = data.codes || [];
      const failN = allCodes.filter((c) => c.status === "FAIL_SEEN").length;
      const passN = allCodes.filter((c) => c.status === "PASS_ONLY").length;
      const naN = allCodes.length - failN - passN;
      subtitle.textContent = `${allCodes.length} Appendix E codes checked: ${failN} with failures, ${passN} fully passed, ${naN} not exercised by this data.`;
      renderCodesTable();
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">Could not load error code detail. The summary CSV downloads above still have this data.</td></tr>`;
    }
  }

  function renderCodesTable() {
    const tbody = document.getElementById("codes-table-body");
    const filtered = allCodes.filter((c) => {
      if (codesFilter === "fail") return c.status === "FAIL_SEEN";
      if (codesFilter === "pass") return c.status === "PASS_ONLY";
      return true;
    });
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">No codes match this filter.</td></tr>`;
      return;
    }
    tbody.innerHTML = filtered.map(codeRowHtml).join("");
  }

  function codeRowHtml(c) {
    const statusClass = c.status === "FAIL_SEEN" ? "fail" : c.status === "PASS_ONLY" ? "pass" : "na";
    const statusLabel = c.status === "FAIL_SEEN" ? "FAIL" : c.status === "PASS_ONLY" ? "PASS" : "N/A";
    const rtypes = (c.resource_types || []).length
      ? `${c.resource_types.join(", ")} (${c.resource_type_count})`
      : "-";
    const examplesHtml = (c.examples || [])
      .map(
        (ex) => `
        <span class="example-line">
          <strong>${escapeHtml(ex.identifier || "")}</strong>
          ${ex.resource_type ? `[${escapeHtml(ex.resource_type)}]` : ""}<br/>
          <span class="expected">expected: ${escapeHtml(ex.expected || "-")}</span><br/>
          <span class="actual">actual: ${escapeHtml(ex.actual || "-")}</span>
        </span>`
      )
      .join("");
    return `
      <tr class="row-${statusClass}">
        <td><span class="code-badge">${c.code}</span><span class="status-pill ${statusClass}">${statusLabel}</span></td>
        <td>${escapeHtml(c.name || "")}</td>
        <td>${escapeHtml(rtypes)}</td>
        <td>${fmt(c.pass_count)}</td>
        <td>${fmt(c.fail_count)}</td>
        <td>${escapeHtml(c.expected_description || "")}</td>
        <td>${examplesHtml || '<span class="muted">-</span>'}</td>
      </tr>`;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  async function startRefCheck() {
    stopPolling();
    document.getElementById("refcheck-contract").textContent = selectedContract;
    document.getElementById("refcheck-status").textContent =
      "Downloading fresh from the live endpoint and checking references...";
    document.getElementById("refcheck-log").textContent = "Starting...";
    document.getElementById("refcheck-summary-wrap").classList.add("hidden");
    document.getElementById("refcheck-rerun-btn").classList.add("hidden");
    showScreen("refcheck");

    try {
      const res = await fetch(`${API_BASE}/api/refcheck/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contract: selectedContract }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || "Could not start the reference check.");
      }
      const data = await res.json();
      currentRefJobId = data.job_id;
      pollRefStatus();
      pollRefLog();
    } catch (err) {
      document.getElementById("refcheck-status").textContent = networkErrorMessage(err);
      document.getElementById("refcheck-rerun-btn").classList.remove("hidden");
    }
  }

  async function pollRefStatus() {
    try {
      const res = await fetch(`${API_BASE}/api/refcheck/jobs/${currentRefJobId}`);
      if (!res.ok) throw new Error("Lost contact with the processing server.");
      const job = await res.json();

      if (job.status === "running") {
        refStatusPollTimer = setTimeout(pollRefStatus, POLL_INTERVAL_MS);
        return;
      }
      if (refLogPollTimer) {
        clearTimeout(refLogPollTimer);
        refLogPollTimer = null;
      }
      await pollRefLog(); // one last fetch to catch the final lines
      if (job.status === "completed") {
        showRefResults(job);
      } else {
        document.getElementById("refcheck-status").textContent =
          job.error || "Reference check failed. Please try again.";
        document.getElementById("refcheck-rerun-btn").classList.remove("hidden");
      }
    } catch (err) {
      document.getElementById("refcheck-status").textContent = networkErrorMessage(err);
      document.getElementById("refcheck-rerun-btn").classList.remove("hidden");
    }
  }

  async function pollRefLog() {
    try {
      const res = await fetch(`${API_BASE}/api/refcheck/jobs/${currentRefJobId}/log`);
      if (res.ok) {
        const text = await res.text();
        const box = document.getElementById("refcheck-log");
        const wasAtBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
        box.textContent = text || "Starting...";
        if (wasAtBottom) box.scrollTop = box.scrollHeight;
      }
    } catch (err) {
      // transient -- next status poll will surface a real error if the job actually died
    }
    if (refStatusPollTimer) {
      refLogPollTimer = setTimeout(pollRefLog, POLL_INTERVAL_MS);
    }
  }

  function showRefResults(job) {
    document.getElementById("refcheck-status").textContent = "Reference check completed successfully.";
    const s = job.summary || {};
    const rows = [
      ["Total Dangling References", fmt(s.total_dangling_refs)],
      ["CSV Files Generated", fmt(s.csv_files_generated)],
    ];
    document.getElementById("refcheck-summary-body").innerHTML = rows
      .map(([label, val]) => `<tr><td>${label}</td><td>${val}</td></tr>`)
      .join("");

    const list = document.getElementById("refcheck-file-list");
    list.innerHTML = "";
    (job.files || []).forEach((filename) => {
      const li = document.createElement("li");
      const link = `${API_BASE}/api/refcheck/jobs/${job.job_id}/files/${encodeURIComponent(filename)}`;
      li.innerHTML = `
        <span class="file-name">${filename}</span>
        <a class="btn btn-primary" href="${link}" target="_blank" rel="noopener">Download</a>
      `;
      list.appendChild(li);
    });

    document.getElementById("refcheck-summary-wrap").classList.remove("hidden");
    document.getElementById("refcheck-rerun-btn").classList.remove("hidden");
  }

  function showError(message) {
    stopPolling();
    document.getElementById("error-message").textContent = message;
    showScreen("error");
  }

  function networkErrorMessage(err) {
    const raw = (err && err.message) || "";
    if (/failed to fetch|networkerror|load failed/i.test(raw)) {
      return "Could not reach the processing server. It may be waking up from idle (free hosting sleeps " +
        "after inactivity) or restarting after handling a very large file -- wait a minute and try again.";
    }
    return raw || "Something went wrong. Please try again.";
  }

  function fmt(n) {
    if (n === null || n === undefined) return "-";
    return Number(n).toLocaleString();
  }

  document.querySelectorAll(".codes-filter .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".codes-filter .chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      codesFilter = btn.dataset.filter;
      renderCodesTable();
    });
  });

  document.querySelectorAll(".mode-card").forEach((card) => {
    card.addEventListener("click", () => openMode(card.dataset.mode));
  });

  document.getElementById("start-btn").addEventListener("click", startProcessing);
  document.getElementById("retry-btn").addEventListener("click", startProcessing);
  document.getElementById("refcheck-btn").addEventListener("click", startRefCheck);
  document.getElementById("refcheck-rerun-btn").addEventListener("click", startRefCheck);

  document.querySelectorAll('[data-action="back-to-select"]').forEach((btn) => {
    btn.addEventListener("click", () => {
      stopPolling();
      selectedContract = null;
      currentJobId = null;
      currentRefJobId = null;
      renderContractGrid();
      showScreen("select");
    });
  });

  document.querySelectorAll('[data-action="back-to-home"]').forEach((btn) => {
    btn.addEventListener("click", () => {
      stopPolling();
      currentMode = null;
      selectedContract = null;
      currentJobId = null;
      currentRefJobId = null;
      showScreen("home");
    });
  });

  loadContracts();
  showScreen("home");
})();
