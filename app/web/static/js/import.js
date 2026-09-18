// /static/js/import.js
//
// CLEAR — Import Workflow
// ─────────────────────────────────────────────

const classesBySide = window.CLASSES_BY_SIDE || {};
let catalog = null;
let previewId = null;
let importerJson = null;
let isNavImport = false;
let allTransactions = [];
let currentPage = 1;
let pageSize = 25;

// ─────────────────────────────────────────────
// Load importers catalog
// ─────────────────────────────────────────────
async function loadCatalog() {
  const res = await fetch("/api/import/catalog");
  catalog = await res.json();
}

// ─────────────────────────────────────────────
// Dropdown population helpers
// ─────────────────────────────────────────────
function setClassOptions(side) {
  const sel = document.getElementById("account_class");
  sel.innerHTML = "";

  if (!side || !classesBySide[side]) {
    sel.disabled = true;
    sel.innerHTML = `<option value="" disabled selected>No classes</option>`;
    return;
  }

  sel.disabled = false;
  sel.insertAdjacentHTML("beforeend", `<option value="" disabled selected>Select...</option>`);

  for (const cls of classesBySide[side]) {
    const o = document.createElement("option");
    o.value = cls;

    // ⭐ Use backend-defined labels
    const label = window.CLASS_LABELS?.[cls] || cls.replace("_", " ").toUpperCase();
    o.textContent = label;

    sel.appendChild(o);
  }
}

async function setImporterOptions(side, cls) {
  const sel = document.getElementById("importer_key");
  sel.innerHTML = "";
  if (!side || !cls) {
    sel.disabled = true;
    sel.innerHTML = `<option value="" disabled selected>Select account class first</option>`;
    return;
  }
  const res = await fetch(`/api/import/catalog?account_side=${side}&account_class=${cls}`);
  const json = await res.json();
  const items = json.importers || [];
  if (!items.length) {
    sel.disabled = true;
    sel.innerHTML = `<option value="" disabled selected>No importers for ${cls}</option>`;
    return;
  }
  sel.disabled = false;
  sel.insertAdjacentHTML("beforeend", `<option value="" disabled selected>Select...</option>`);
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.key;
    o.textContent = it.name;
    o.dataset.formats = (it.formats || []).join(",");
    o.dataset.institution = it.institution_default || "";
    sel.appendChild(o);
  }
}

function updateFileHint() {
  const sel = document.getElementById("importer_key");
  const opt = sel.options[sel.selectedIndex];
  const formats = opt?.dataset?.formats || "pdf";
  document.getElementById("file_hint").textContent = `Upload a ${formats.toUpperCase()} file.`;
}

// ─────────────────────────────────────────────
// Summary helpers
// ─────────────────────────────────────────────
function showAccountSummary(acc) {
  const box = document.getElementById("accountSummary");
  box.classList.remove("d-none");
  document.getElementById("accID").textContent = acc.account_id || "";
  document.getElementById("accName").textContent = acc.account_name || "";
  document.getElementById("accInst").textContent = acc.institution || "";
  document.getElementById("accSide").textContent = acc.account_side || "";
  document.getElementById("accClass").textContent = acc.account_class || "";
}

function summarizeByType(transactions) {
  const totals = { expense: 0, income: 0, refund: 0, transfer: 0 };
  transactions.forEach(tx => {
    const amt = tx.amount || 0;
    if (tx.transaction_type === "expense") totals.expense += amt;
    else if (tx.transaction_type === "income") totals.income += amt;
    else if (tx.transaction_type === "refund") totals.refund += amt;
    else if (tx.transaction_type === "transfer") totals.transfer += amt;
  });
  return totals;
}

function showNavPreview(navs) {
  const navBody = document.getElementById("navPreviewBody");
  navBody.innerHTML = "";
  navs.forEach(ns => {
    navBody.insertAdjacentHTML("beforeend", `
      <tr>
        <td>${ns.account_id || ""}</td>
        <td>${ns.account_name || ""}</td>
        <td>${ns.institution || ""}</td>
        <td>${ns.date || ""}</td>
        <td>${parseFloat(ns.nav || 0).toLocaleString("en-CA", { style: "currency", currency: ns.base_currency || "CAD" })}</td>
      </tr>
    `);
  });
}

// ─────────────────────────────────────────────
// Main summary render
// ─────────────────────────────────────────────
function showSummary(data) {
  document.getElementById("importWizard").classList.remove("d-none");
  document.getElementById("stageSummary").classList.remove("d-none");
  document.getElementById("stageTransactions").classList.add("d-none");

  const summaryDiv = document.getElementById("summaryDetails");
  summaryDiv.innerHTML = "";

  if (isNavImport) {
    showAccountSummary(data.account_summary);
    summaryDiv.insertAdjacentHTML("beforeend", `
      <div class="p-2 border rounded">
        <b>Validation:</b> ${data.importer_json.validation_passed ? "✅ Passed" : "⚠️ Failed"}<br>
        <small>${data.importer_json.validation_message || ""}</small>
      </div>
    `);
    document.getElementById("nextBtn").classList.remove("d-none");
    document.getElementById("nextBtn").textContent = "View NAV Snapshot";
    return;
  }

  data.importer_json.statements.forEach((stmt, idx) => {
    const totals = summarizeByType(stmt.transactions || []);
    const closing = stmt.summary?.closing_balance;

    summaryDiv.insertAdjacentHTML("beforeend", `
      <div class="mb-3 p-2 border rounded">
        <h6>Account ${idx + 1}: ${stmt.cardholder_info.name} (${stmt.cardholder_info.card_digits})</h6>
        <p><b>Total tx:</b> ${stmt.transactions.length}</p>
        <p>
          <b>Expenses:</b> ${totals.expense.toLocaleString("en-CA", { style: "currency", currency: "CAD" })}<br>
          <b>Income:</b> ${totals.income.toLocaleString("en-CA", { style: "currency", currency: "CAD" })}<br>
          <b>Refunds:</b> ${totals.refund.toLocaleString("en-CA", { style: "currency", currency: "CAD" })}<br>
          <b>Transfers:</b> ${totals.transfer.toLocaleString("en-CA", { style: "currency", currency: "CAD" })}<br>
          ${
            closing !== null && closing !== undefined
              ? `<b>Closing balance:</b> ${parseFloat(closing).toLocaleString("en-CA", { style: "currency", currency: "CAD" })}`
              : ""
          }
        </p>
        <p>
          <b>Validation:</b> ${stmt.summary.validation_passed ? "✅ Passed" : "⚠️ Failed"}<br>
          <small class="text-muted">${stmt.summary.validation_message || ""}</small>
        </p>
      </div>
    `);
  });
}

// ─────────────────────────────────────────────
// Transactions pagination + preview
// ─────────────────────────────────────────────
function flattenTransactions() {
  allTransactions = [];
  importerJson.statements.forEach(stmt => {
    (stmt.transactions || []).forEach(tx => {
      const amt = tx.amount;
      allTransactions.push({
        cardholder: stmt.cardholder_info.name,
        date: tx.operation_date,
        desc: tx.description,
        debit: tx.debit || "",
        credit: tx.credit || "",
        signed: amt,
        type: tx.transaction_type
      });
    });
  });
}

function renderTransactions() {
  const txBody = document.querySelector("#txTable tbody");
  txBody.innerHTML = "";
  const startIdx = (currentPage - 1) * pageSize;
  const pageTx = allTransactions.slice(startIdx, startIdx + pageSize);
  pageTx.forEach(tx => {
    txBody.insertAdjacentHTML("beforeend", `
      <tr>
        <td>${tx.cardholder}</td>
        <td>${tx.date}</td>
        <td>${tx.desc}</td>
        <td>${tx.debit}</td>
        <td>${tx.credit}</td>
        <td>${tx.signed}</td>
        <td>${tx.type}</td>
      </tr>
    `);
  });
  renderPagination();
}

function renderPagination() {
  const totalPages = Math.ceil(allTransactions.length / pageSize);
  const container = document.getElementById("paginationControls");
  container.innerHTML = "";
  for (let i = 1; i <= totalPages; i++) {
    const btn = document.createElement("button");
    btn.textContent = i;
    btn.className = `btn btn-sm ${i === currentPage ? "btn-primary" : "btn-outline-primary"}`;
    btn.addEventListener("click", () => {
      currentPage = i;
      renderTransactions();
    });
    container.appendChild(btn);
  }
}

function showTransactions() {
  document.getElementById("stageSummary").classList.add("d-none");
  document.getElementById("stageTransactions").classList.remove("d-none");
  const txTable = document.getElementById("txTable");
  const navDiv = document.getElementById("navPreview");
  const title = document.getElementById("previewTitle");

  if (isNavImport) {
    txTable.style.display = "none";
    navDiv.classList.remove("d-none");
    title.textContent = "NAV Snapshot Preview";
    showNavPreview(window.lastPreviewData.nav_preview || []);
    document.getElementById("commitBtn").textContent = "Approve & Import NAV";
    document.getElementById("pageSize").closest("div").style.display = "none";
    document.getElementById("paginationControls").style.display = "none";
  } else {
    navDiv.classList.add("d-none");
    txTable.style.display = "";
    title.textContent = "Transactions Preview";
    document.getElementById("commitBtn").textContent = "Approve & Import";
    document.getElementById("pageSize").closest("div").style.display = "";
    document.getElementById("paginationControls").style.display = "";
    flattenTransactions();
    renderTransactions();
  }
}

// ─────────────────────────────────────────────
// Events
// ─────────────────────────────────────────────
document.getElementById("account_side").addEventListener("change", e => {
  setClassOptions(e.target.value);
  document.getElementById("importer_key").disabled = true;
});

document.getElementById("account_class").addEventListener("change", e => {
  const side = document.getElementById("account_side").value;
  setImporterOptions(side, e.target.value);
});

document.getElementById("importer_key").addEventListener("change", e => {
  updateFileHint();
  const opt = e.target.options[e.target.selectedIndex];
  const inst = opt?.dataset?.institution || "";
  if (inst) document.querySelector('input[name="institution"]').value = inst;
});

document.getElementById("importForm").addEventListener("submit", async e => {
  e.preventDefault();
  window.lastPreviewData = null;
  const form = e.target;
  const data = new FormData(form);
  document.getElementById("status").textContent = "Parsing...";
  const res = await fetch("/api/import/preview", { method: "POST", body: data });
  const json = await res.json().catch(() => ({}));
  document.getElementById("status").textContent = "";
  if (!json.ok) {
    alert("Error: " + (json.error || "Unknown"));
    return;
  }
  previewId = json.preview_id;
  importerJson = json.importer_json;
  isNavImport = json.is_nav_import;
  window.lastPreviewData = json;
  showSummary(json);
});

document.getElementById("nextBtn").addEventListener("click", showTransactions);
document.getElementById("backBtn").addEventListener("click", () => {
  document.getElementById("stageTransactions").classList.add("d-none");
  document.getElementById("stageSummary").classList.remove("d-none");
});

document.getElementById("commitBtn").addEventListener("click", async () => {
  let payload = { preview_id: previewId };

  if (window.lastPreviewData?.already_imported) {
    const ok = confirm(
      "⚠️ This statement was already imported.\n\n" +
      "Re-importing will DELETE and REPLACE all transactions from this statement,\n" +
      "including their type (expense / transfer / income).\n\n" +
      "Do you want to overwrite?"
    );
    if (!ok) return;

    payload._overwrite_statement = true;
  }

  const res = await fetch("/api/import/commit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const json = await res.json();
  alert(json.ok ? "Import successful!" : "Import failed: " + (json.error || "Unknown"));
  if (json.ok) location.href = "/";
});

document.getElementById("rejectBtn").addEventListener("click", async () => {
  await fetch("/api/import/reject", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preview_id: previewId })
  });
  alert("Import rejected.");
  location.reload();
});

document.getElementById("pageSize").addEventListener("change", e => {
  pageSize = parseInt(e.target.value);
  currentPage = 1;
  renderTransactions();
});

loadCatalog();
