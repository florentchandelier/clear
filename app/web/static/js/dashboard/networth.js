// /static/js/dashboard/networth.js

let liquidAssetsChart = null;

/* ─────────────────────────────────────────────
   Section A — Net Worth Summary
───────────────────────────────────────────── */
async function renderNetWorthSummary() {
  const el = document.getElementById("netWorthValue");
  if (!el) return;

  const res = await fetch("/api/net_worth/series_fx", { cache: "no-store" });
  const payload = await res.json();
  const rows = payload.series;
  if (!rows.length) return;

  const latest = rows[rows.length - 1];
  el.textContent = formatCurrency(latest.net_worth);
}

/* ─────────────────────────────────────────────
   Section B — Liquid Financial Assets
───────────────────────────────────────────── */
async function renderLiquidAssetsChart(year) {
  const canvas = document.getElementById("liquidAssetsChart");
  if (!canvas) return;

  const res = await fetch(`/api/net_worth/liquid_assets_fx?year=${encodeURIComponent(year)}`,
  { cache: "no-store" }
  );
  const rows = await res.json();
  if (!Array.isArray(rows) || !rows.length) return;

  // ─────────────────────────────────────────────
  // Normalize months
  // ─────────────────────────────────────────────
  const months = [...new Set(rows.map(r => r.month))].sort();

  // ─────────────────────────────────────────────
  // Aggregate by month
  // ─────────────────────────────────────────────
  const cash = {};
  const investment = {};
  const liabilities = {};

  months.forEach(m => {
    cash[m] = 0;
    investment[m] = 0;
    liabilities[m] = 0;
  });

  rows.forEach(r => {
    if (r.balance == null) return;

    if (r.account_class === "cash") {
      cash[r.month] += r.balance;
    } else if (r.account_class === "investment") {
      investment[r.month] += r.balance;
    } else if (r.account_class === "liability") {
      // liabilities plotted as negative
      liabilities[r.month] -= Math.abs(r.balance);
    }
  });

  // ─────────────────────────────────────────────
  // Compute total liquid assets (net)
  // ─────────────────────────────────────────────
  const total = months.map(m =>
    (cash[m] || 0) + (investment[m] || 0) + (liabilities[m] || 0)
  );

  // ─────────────────────────────────────────────
  // Build datasets
  // ─────────────────────────────────────────────
  const datasets = [
    {
      label: "Cash",
      data: months.map(m => cash[m]),
      borderColor: "rgb(54, 162, 235)",
      backgroundColor: "rgba(54, 162, 235, 0.35)",
      fill: true,
      stack: "assets",
      tension: 0.25
    },
    {
      label: "Investments",
      data: months.map(m => investment[m]),
      borderColor: "rgb(75, 192, 192)",
      backgroundColor: "rgba(75, 192, 192, 0.35)",
      fill: true,
      stack: "assets",
      tension: 0.25
    },
    {
      label: "Outstanding Liabilities",
      data: months.map(m => liabilities[m]),
      borderColor: "rgb(255, 99, 132)",
      backgroundColor: "rgba(255, 99, 132, 0.25)",
      fill: true,
      stack: "liabilities",
      tension: 0.25
    },
    {
      label: "Total Liquid Assets",
      data: total,
      type: "line",
      borderColor: "rgb(33, 37, 41)",
      borderWidth: 2.5,
      pointRadius: 0,
      fill: false,
      tension: 0.2
    }
  ];

  // ─────────────────────────────────────────────
  // Render chart
  // ─────────────────────────────────────────────
  if (liquidAssetsChart) liquidAssetsChart.destroy();

  liquidAssetsChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: months,
      datasets
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "bottom"
        },
        tooltip: {
          callbacks: {
            title: items => items[0].label,
            label: ctx =>
              `${ctx.dataset.label}: ${formatCurrency(ctx.parsed.y)}`
          }
        }
      },
      scales: {
        y: {
          stacked: true,
          ticks: {
            callback: v => formatCurrency(v)
          }
        }
      }
    }
  });
}

/* ─────────────────────────────────────────────
   Section D — Real Assets (Snapshot table)
───────────────────────────────────────────── */
async function renderRealAssetsTable() {
  const tbody = document.getElementById("realAssetsTable");
  if (!tbody) return;

  const res = await fetch("/api/net_worth/real_assets_fx", { cache: "no-store" });
  const rows = await res.json();

  if (!Array.isArray(rows) || !rows.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="3" class="text-muted text-center small">
          No real assets recorded
        </td>
      </tr>`;
    return;
  }

  tbody.innerHTML = "";

  rows.forEach(r => {
    tbody.insertAdjacentHTML(
      "beforeend",
      `<tr>
        <td>${r.name ?? "—"}</td>
        <td>${r.date ?? "—"}</td>
        <td class="text-end">${formatCurrency(r.value)}</td>
      </tr>`
    );
  });
}

/* ─────────────────────────────────────────────
   Section E — Asset Mix
───────────────────────────────────────────── */
async function renderAssetMatrix() {
  const tbody = document.getElementById("assetMatrixTable");
  if (!tbody) return;

  const res = await fetch("/api/net_worth/asset_matrix_fx", { cache: "no-store" });
  const rows = await res.json();

  if (!Array.isArray(rows) || !rows.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="3" class="text-muted text-center small">
          No data
        </td>
      </tr>`;
    return;
  }

  tbody.innerHTML = "";

  rows.forEach(r => {
    tbody.insertAdjacentHTML(
      "beforeend",
      `<tr>
        <td>${r.asset_nature ?? "—"}</td>
        <td>${r.liquidity_class ?? "—"}</td>
        <td class="text-end">${formatCurrency(r.balance)}</td>
      </tr>`
    );
  });
}

/* ─────────────────────────────────────────────
   Public entry point (called by dashboard.js)
───────────────────────────────────────────── */
async function renderNetWorthChart() {
  const year = document.getElementById("yearSelect")?.value;
  await renderNetWorthSummary();
  await renderLiquidAssetsChart(year);
  await renderRealAssetsTable();
  await renderAssetMatrix();

  document
    .querySelectorAll("#view-networth input.form-check-input")
    .forEach(cb =>
      cb.addEventListener("change", renderLiquidAssetsChart)
    );
}

/* ─────────────────────────────────────────────
   Formatting (temporary — will centralize next)
───────────────────────────────────────────── */
function formatCurrency(v) {
  if (v == null || isNaN(v)) return "—";
  return new Intl.NumberFormat("en-CA", {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0
  }).format(v);
}
