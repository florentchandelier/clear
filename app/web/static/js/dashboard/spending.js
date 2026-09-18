// app/web/static/js/dashboard/spending.js
let monthlyChart = null;
let categoryChart = null;
let stackedSpendingChart = null;

// ─────────────────────────────────────────────
// 🔹 Render Monthly Chart (Spending by Month)
// ─────────────────────────────────────────────
async function renderMonthlyChart(year) {
  const canvas = document.getElementById("monthlyChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const res = await fetch(`/api/dashboard/spending_by_month_for_year?year=${encodeURIComponent(year)}`);
  const chartData = await res.json();

  if (monthlyChart) monthlyChart.destroy();
  monthlyChart = new Chart(ctx, {
    type: "bar",
    data: chartData,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      resizeDelay: 150,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { mode: "index", intersect: false },
      },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, ticks: { precision: 0 } },
      },
    },
  });
}

// ─────────────────────────────────────────────
// 🔹 Render Category or Tag Chart
// ─────────────────────────────────────────────
async function renderCategoryChart(view = "category", year = null) {
  const canvas = document.getElementById("categoryChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const baseUrl =
    view === "tag"
      ? "/api/dashboard/total_spending_by_tag"
      : "/api/dashboard/total_spending_by_category";
  const url = year == null ? baseUrl : `${baseUrl}?year=${encodeURIComponent(year)}`;

  const res = await fetch(url);
  const data = await res.json();

  if (categoryChart) categoryChart.destroy();
  categoryChart = new Chart(ctx, {
    type: "doughnut",
    data: data.chart,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      resizeDelay: 150,
      animation: false,
      plugins: { legend: { position: "bottom" } },
    },
  });

  renderTopTable(data.table);
}

// ─────────────────────────────────────────────
// 🔹 Render Stacked Spending Chart
// ─────────────────────────────────────────────
async function renderStackedSpendingChart(year) {
  const canvas = document.getElementById("stackedSpendingChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const res = await fetch(`/api/dashboard/spending_stacked_by_category?year=${encodeURIComponent(year)}`);
  const data = await res.json();

  if (stackedSpendingChart) stackedSpendingChart.destroy();
  stackedSpendingChart = new Chart(ctx, {
    type: "bar",
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      resizeDelay: 150,
      animation: false,
      plugins: { legend: { position: "bottom" } },
      scales: {
        x: { stacked: true },
        y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
      },
    },
  });
}

// ─────────────────────────────────────────────
// 🔹 Render Top 10 Table
// ─────────────────────────────────────────────
function renderTopTable(rows) {
  const tbody = document.getElementById("topCategoriesBody");
  if (!tbody) return;
  tbody.innerHTML = "";
  (rows || []).forEach(r => {
    const name = r.name || "other";
    const val = Math.trunc(Number(r.total_spent || 0)).toLocaleString();
    tbody.insertAdjacentHTML(
      "beforeend",
      `<tr>
         <td><span class="badge bg-secondary-subtle text-secondary-emphasis">${name}</span></td>
         <td class="text-end">${val}</td>
       </tr>`
    );
  });
}
