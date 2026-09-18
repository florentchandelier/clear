// ─────────────────────────────────────────────────────
// Transactions View: Sorting, Filtering, Table & Editing
// ─────────────────────────────────────────────────────
let allRows = [];
let categoriesTree = {};
let currentSortKey = "operation_date";
let currentSortAsc = false;

// ────────────────────────────────
// Column Preferences
// ────────────────────────────────
let visibleColumns = new Set(JSON.parse(localStorage.getItem("visibleColumns") || "[]"));
if (!visibleColumns.size) {
  visibleColumns = new Set([
    "operation_date","account_name","description","description_norm",
    "amount","category","subcategory","type","transaction_type"
  ]);
}

// ────────────────────────────────
// Helpers
// ────────────────────────────────
function val(id, v) {
  const el = document.getElementById(id);
  if (!el) return "";
  if (v !== undefined) el.value = v;
  return el.value;
}

// ────────────────────────────────
// Fetch categories tree for editing
// ────────────────────────────────
async function fetchCategoriesTree() {
  try {
    const res = await fetch("/api/categories/tree");
    categoriesTree = res.ok ? await res.json() : {};
  } catch {
    categoriesTree = {};
  }
}

// ────────────────────────────────
// Autocomplete for filter bar
// ────────────────────────────────
async function setupAutocomplete() {
  const endpoints = {
    category: "/api/categories",
    subcategory: "/api/subcategories",
    type: "/api/types",
    account: "/api/accounts"
  };

  for (const [id, url] of Object.entries(endpoints)) {
    const inputId = `filter-${id}`;
    const input = document.getElementById(inputId);
    if (!input) continue;

    try {
      const res = await fetch(url);
      const list = res.ok ? await res.json() : [];
      console.log(`Loaded ${list.length} ${id} options`);

      const ac = new autoComplete({
        selector: `#${inputId}`,
        data: { src: list },
        threshold: 1,
        resultItem: { highlight: true },
        placeHolder: `Search ${id}...`,
        events: {
          input: {
            selection: (event) => {
              const value = event.detail.selection.value;
              input.value = value;
              ac.close();
            }
          }
        }
      });
    } catch (err) {
      console.error(`❌ Autocomplete failed for ${id}:`, err);
    }
  }
}

// ────────────────────────────────
// Table Rendering
// ────────────────────────────────
function renderTable() {
  const tbody = document.getElementById("txTableBody");
  if (!allRows.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="text-muted text-center">No transactions</td></tr>`;
    return;
  }

  const rows = [...allRows].sort((a, b) => {
    const A = a[currentSortKey] ?? "";
    const B = b[currentSortKey] ?? "";
    if (currentSortKey === "amount") return currentSortAsc ? A - B : B - A;
    if (currentSortKey.includes("date"))
      return currentSortAsc ? new Date(A) - new Date(B) : new Date(B) - new Date(A);
    return currentSortAsc ? String(A).localeCompare(String(B)) : String(B).localeCompare(String(A));
  });

  tbody.innerHTML = rows.map(r => `
    <tr data-tx='${JSON.stringify(r)}'>
      <td><input type="checkbox" class="tx-select" value="${r.transaction_id}"></td>
      <td>${new Date(r.operation_date).toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"numeric"})}</td>
      <td>${r.account_name || ""}</td>
      <td>${r.description || ""}</td>
      <td>${r.description_norm || ""}</td>
      <td class="text-end">${(r.amount || 0).toFixed(2)}</td>
      <td class="cat-cell">${r.category || "uncategorized"}</td>
      <td class="subcat-cell">${r.subcategory || ""}</td>
      <td class="type-cell">${r.type || ""}</td>
      <td>${r.transaction_type || ""}</td>
      <td><button class="btn btn-sm btn-outline-primary edit-btn">Edit</button></td>
    </tr>`).join("");

  updateColumnVisibility();
  setupSelection();
  document.querySelectorAll(".edit-btn").forEach(btn => btn.addEventListener("click", () => toggleEdit(btn)));
}

// ────────────────────────────────
// Bulk Selection & Categorization
// ────────────────────────────────
function selectedTxIds() {
  return [...document.querySelectorAll(".tx-select:checked")].map(el => el.value);
}

function updateBulkBar() {
  const n = selectedTxIds().length;
  const bar = document.getElementById("bulkBar");
  const count = document.getElementById("bulkCount");
  if (!bar || !count) return;

  count.textContent = `${n} selected`;
  if (n > 0) bar.classList.remove("d-none");
  else bar.classList.add("d-none");
}

// Set up checkboxes after table render
function setupSelection() {
  const chkAll = document.getElementById("chkAll");
  chkAll.onchange = () => {
    document.querySelectorAll(".tx-select").forEach(cb => (cb.checked = chkAll.checked));
    updateBulkBar();
  };
  document.querySelectorAll(".tx-select").forEach(cb => {
    cb.addEventListener("change", () => {
      if (!cb.checked) chkAll.checked = false;
      updateBulkBar();
    });
  });
}

// Populate cascading selects in bulk bar
function populateBulkPickers() {
  const catSel = document.getElementById("bulkCat");
  const subSel = document.getElementById("bulkSub");
  const typeSel = document.getElementById("bulkType");

  const cats = Object.keys(categoriesTree).filter(k => !["_seeds","_deleted"].includes(k));
  catSel.innerHTML = `<option value="">Select category</option>` +
    cats.map(c => `<option value="${c}">${c}</option>`).join("");

  catSel.onchange = () => {
    const cat = catSel.value;
    const subs = Object.keys(categoriesTree[cat] || {}).filter(k => !["_seeds","_deleted"].includes(k));
    subSel.innerHTML = `<option value="">Subcategory</option>` +
      subs.map(s => `<option value="${s}">${s}</option>`).join("");
    typeSel.innerHTML = `<option value="">Type</option>`;
  };

  subSel.onchange = () => {
    const cat = catSel.value, sub = subSel.value;
    const types = Object.keys((categoriesTree[cat] || {})[sub] || {}).filter(k => !["_seeds","_deleted"].includes(k));
    typeSel.innerHTML = `<option value="">Type</option>` +
      types.map(t => `<option value="${t}">${t}</option>`).join("");
  };
}

// Send bulk categorize request
async function applyBulkCategorization() {
  const ids = selectedTxIds();
  if (!ids.length) return;

  const cat = document.getElementById("bulkCat").value;
  if (!cat) { alert("Please choose a category."); return; }

  const sub = document.getElementById("bulkSub").value || null;
  const typ = document.getElementById("bulkType").value || null;
  const mode = document.querySelector("input[name='bulkMode']:checked").value;

  const res = await fetch("/api/transactions/bulk-categorize", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ tx_ids: ids, category: cat, subcategory: sub, type: typ, mode })
  });

  const json = await res.json();
  if (json.ok) {
    const toast = document.createElement("div");
    toast.className = "position-fixed bottom-0 start-50 translate-middle-x bg-success text-white px-3 py-2 rounded shadow";
    toast.textContent = json.message || "Bulk update successful!";
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);

    await loadTransactions();

    // Reset cascading selectors
    const catSel = document.getElementById("bulkCat");
    const subSel = document.getElementById("bulkSub");
    const typeSel = document.getElementById("bulkType");
    if (catSel && subSel && typeSel) {
      catSel.value = "";
      subSel.innerHTML = "<option value=''>Subcategory</option>";
      typeSel.innerHTML = "<option value=''>Type</option>";
    }
  } else {
    alert("Error: " + (json.error || "Failed to apply bulk categorize."));
  }

  document.getElementById("chkAll").checked = false;
  updateBulkBar();
}

// Hook up events
document.addEventListener("change", e => {
  if (e.target.classList.contains("tx-select")) updateBulkBar();
});
document.getElementById("bulkApply").onclick = applyBulkCategorization;

// Cancel bulk action
document.getElementById("bulkCancel").onclick = () => {
  // Uncheck all checkboxes
  document.querySelectorAll(".tx-select").forEach(cb => (cb.checked = false));
  document.getElementById("chkAll").checked = false;

  // Reset dropdowns
  const catSel = document.getElementById("bulkCat");
  const subSel = document.getElementById("bulkSub");
  const typeSel = document.getElementById("bulkType");
  if (catSel && subSel && typeSel) {
    catSel.value = "";
    subSel.innerHTML = "<option value=''>Subcategory</option>";
    typeSel.innerHTML = "<option value=''>Type</option>";
  }

  // Hide bulk bar
  document.getElementById("bulkBar").classList.add("d-none");

  // Update selection count
  updateBulkBar();
};

// Sticky effect
window.addEventListener("scroll", () => {
  const bulkBar = document.getElementById("bulkBar");
  if (!bulkBar) return;
  const rect = bulkBar.getBoundingClientRect();
  if (rect.top <= 65 && !bulkBar.classList.contains("is-stuck")) {
    bulkBar.classList.add("is-stuck");
  } else if (rect.top > 65 && bulkBar.classList.contains("is-stuck")) {
    bulkBar.classList.remove("is-stuck");
  }
});

// ────────────────────────────────
// Unified Edit Behavior → Activate Bulk Bar
// ────────────────────────────────
function toggleEdit(btn) {
  const tr = btn.closest("tr");
  const tx = JSON.parse(tr.dataset.tx);
  const chk = tr.querySelector(".tx-select");
  const bulkBar = document.getElementById("bulkBar");

  chk.checked = true;
  updateBulkBar();

  const catSel = document.getElementById("bulkCat");
  const subSel = document.getElementById("bulkSub");
  const typeSel = document.getElementById("bulkType");

  if (tx.category && categoriesTree[tx.category]) {
    catSel.value = tx.category;

    const subs = Object.keys(categoriesTree[tx.category] || {}).filter(k => !["_seeds","_deleted"].includes(k));
    subSel.innerHTML = `<option value="">Subcategory</option>` +
      subs.map(s => `<option value="${s}" ${tx.subcategory === s ? "selected" : ""}>${s}</option>`).join("");

    if (tx.subcategory && categoriesTree[tx.category][tx.subcategory]) {
      const types = Object.keys(categoriesTree[tx.category][tx.subcategory]).filter(k => !["_seeds","_deleted"].includes(k));
      typeSel.innerHTML = `<option value="">Type</option>` +
        types.map(t => `<option value="${t}" ${tx.type === t ? "selected" : ""}>${t}</option>`).join("");
    } else {
      typeSel.innerHTML = `<option value="">Type</option>`;
    }
  }

  bulkBar.classList.remove("d-none");
  bulkBar.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ────────────────────────────────
// Sorting
// ────────────────────────────────
function setupSorting() {
  document.querySelectorAll("#transactionsTable th[data-key]").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      currentSortAsc = currentSortKey === key ? !currentSortAsc : true;
      currentSortKey = key;
      document.querySelectorAll(".sort-icon").forEach(i => (i.textContent = "↕"));
      th.querySelector(".sort-icon").textContent = currentSortAsc ? "▴" : "▾";
      renderTable();
    });
  });
}

// ────────────────────────────────
// Column Visibility
// ────────────────────────────────
function setupColumnToggles() {
  const btn = document.getElementById("toggleColumnsBtn");
  const dropdown = document.getElementById("columnsDropdown");
  const box = document.getElementById("columnsCheckboxes");
  const keys = [
    "operation_date","account_name","description","description_norm",
    "amount","category","subcategory","type","transaction_type"
  ];

  box.innerHTML = keys.map(k => `
    <div class="form-check">
      <input class="form-check-input" type="checkbox" value="${k}" ${visibleColumns.has(k) ? "checked" : ""}>
      <label class="form-check-label small">${k}</label>
    </div>`).join("");

  btn.onclick = () => (dropdown.style.display = dropdown.style.display === "none" ? "block" : "none");
  document.addEventListener("click", e => {
    if (!dropdown.contains(e.target) && !btn.contains(e.target)) dropdown.style.display = "none";
  });

  box.querySelectorAll("input").forEach(chk => {
    chk.onchange = e => {
      if (e.target.checked) visibleColumns.add(e.target.value);
      else visibleColumns.delete(e.target.value);
      localStorage.setItem("visibleColumns", JSON.stringify([...visibleColumns]));
      updateColumnVisibility();
    };
  });
}

function updateColumnVisibility() {
  const ths = document.querySelectorAll("#transactionsTable th[data-key]");
  const trs = document.querySelectorAll("#transactionsTable tbody tr");
  ths.forEach((th, i) => {
    const visible = visibleColumns.has(th.dataset.key),
      idx = i + 1;
    th.style.display = visible ? "" : "none";
    trs.forEach(tr => {
      const td = tr.querySelector(`td:nth-child(${idx})`);
      if (td) td.style.display = visible ? "" : "none";
    });
  });
}

// ────────────────────────────────
// Year / Month / Account Loading
// ────────────────────────────────
async function loadYearsAndAccounts() {
  const [yearsRes, latestRes, accountsRes] = await Promise.all([
    fetch("/api/transactions/years"),
    fetch("/api/transactions/latest"),
    fetch("/api/transactions/accounts")
  ]);

  const years = await yearsRes.json();
  const latest = await latestRes.json();
  const accounts = await accountsRes.json();

  const yearSel = document.getElementById("yearSelect");
  const monthSel = document.getElementById("monthSelect");
  const accountSel = document.getElementById("accountSelect");

  yearSel.innerHTML = years.map(y => `<option value="${y}">${y}</option>`).join("");
  yearSel.value = latest.year || years[0];
  await updateMonthsForYear(yearSel.value);
  if (latest.month) monthSel.value = latest.month;

  accountSel.innerHTML = `<option value="">All</option>` + accounts.map(a => `<option value="${a}">${a}</option>`).join("");
  const savedAcc = localStorage.getItem("selectedAccount") || "";
  if (savedAcc && accounts.includes(savedAcc)) accountSel.value = savedAcc;

  yearSel.onchange = async () => {
    await updateMonthsForYear(yearSel.value);
    monthSel.value = monthSel.options[monthSel.options.length - 1].value;
    await loadTransactions();
  };
  monthSel.onchange = loadTransactions;
  accountSel.onchange = () => {
    localStorage.setItem("selectedAccount", accountSel.value);
    loadTransactions();
  };

  await loadTransactions();
}

async function updateMonthsForYear(year) {
  const res = await fetch(`/api/transactions/months_for_year?year=${year}`);
  const months = await res.json();
  const monthSel = document.getElementById("monthSelect");
  monthSel.innerHTML = months.map(m =>
    `<option value="${m}">${new Date(0, m - 1).toLocaleString("default", { month: "short" })}</option>`
  ).join("");
}

// ────────────────────────────────
// Transactions Loading
// ────────────────────────────────
async function loadTransactions() {
  const year = val("yearSelect");
  const month = val("monthSelect");
  const account = val("accountSelect");
  const params = new URLSearchParams({ year, month });
  if (account) params.append("account", account);

  const res = await fetch(`/api/transactions/by_month?${params.toString()}`);
  allRows = await res.json();
  renderTable();
}

// ────────────────────────────────
// Filter Bar
// ────────────────────────────────
document.getElementById("filterBtn").onclick = () => {
  const bar = document.getElementById("filterBar");
  bar.style.display = bar.style.display === "none" ? "block" : "none";
};

document.getElementById("applyFilters").onclick = async () => {
  const params = new URLSearchParams({
    year: val("yearSelect"),
    month: val("monthSelect"),
    category: val("filter-category"),
    subcategory: val("filter-subcategory"),
    type: val("filter-type"),
    account: val("filter-account")
  });

  const res = await fetch(`/api/transactions/filter?${params.toString()}`);
  allRows = await res.json();
  renderTable();
};

document.getElementById("clearFilters").onclick = () => {
  ["category", "subcategory", "type", "account"].forEach(id => val(`filter-${id}`, ""));
  document.getElementById("filterBar").style.display = "none";
  loadTransactions();
};

// ────────────────────────────────
// Initialization
// ────────────────────────────────
window.addEventListener("DOMContentLoaded", async () => {
  await fetchCategoriesTree();
  setupAutocomplete();
  setupSorting();
  setupColumnToggles();
  populateBulkPickers();
  await loadYearsAndAccounts();

  // Enable Bootstrap tooltips
  const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
  tooltipTriggerList.map(el => new bootstrap.Tooltip(el));
});
