(function () {
  const API_BASE = window.API_BASE_URL;
  const POLL_INTERVAL_MS = 4000;

  const screens = {
    select: document.getElementById("screen-select"),
    confirm: document.getElementById("screen-confirm"),
    processing: document.getElementById("screen-processing"),
    results: document.getElementById("screen-results"),
    error: document.getElementById("screen-error"),
  };

  let selectedContract = null;
  let currentJobId = null;
  let pollTimer = null;
  let allCodes = [];
  let codesFilter = "all";

  function showScreen(name) {
    Object.values(screens).forEach((s) => s.classList.add("hidden"));
    screens[name].classList.remove("hidden");
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  async function loadContracts() {
    const grid = document.getElementById("contract-grid");
    try {
      const res = await fetch(`${API_BASE}/api/contracts`);
      if (!res.ok) throw new Error("bad response");
      const data = await res.json();
      grid.innerHTML = "";
      data.contracts.forEach((c) => {
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
    } catch (err) {
      grid.innerHTML = `<p class="muted">Could not reach the processing server. Please check back later.</p>`;
    }
  }

  function selectContract(contract) {
    selectedContract = contract;
    document.getElementById("confirm-contract").textContent = contract;
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
      showError(err.message || "Lost contact with the processing server.");
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

  function showError(message) {
    stopPolling();
    document.getElementById("error-message").textContent = message;
    showScreen("error");
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

  document.getElementById("start-btn").addEventListener("click", startProcessing);
  document.getElementById("retry-btn").addEventListener("click", startProcessing);
  document.querySelectorAll('[data-action="back-to-select"]').forEach((btn) => {
    btn.addEventListener("click", () => {
      stopPolling();
      selectedContract = null;
      currentJobId = null;
      showScreen("select");
    });
  });

  loadContracts();
  showScreen("select");
})();
