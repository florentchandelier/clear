// app/web/static/js/dashboard/income_vs_expense.js

let incomeExpenseChart = null;

// ─────────────────────────────────────────────
// 🔹 Render Income vs Expense Chart (new)
// ─────────────────────────────────────────────
async function renderIncomeExpenseChart(year) {
  const canvas = document.getElementById("incomeExpenseChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const res = await fetch(`/api/dashboard/income_vs_expense_for_year?year=${encodeURIComponent(year)}`);
  const data = await res.json();
  const labels = data.map(r => r.month);

  const income = data.map(r => r.income);
  // Signed expenses for math
  const expenseSigned = data.map(r => r.expense);
  // Absolute expenses for display only
  const expenseDisplay = expenseSigned.map(v => Math.abs(v));
  // Net = income + signed expense (expense is negative)
  const net = income.map((v, i) => v + expenseSigned[i]);

  if (incomeExpenseChart) incomeExpenseChart.destroy();
  incomeExpenseChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
          {
            label: "Income",
            data: income,
            backgroundColor: "rgba(54, 162, 235, 0.7)",
          },
          {
            label: "Expense",
            data: expenseDisplay,
            backgroundColor: "rgba(255, 99, 132, 0.7)",
          },
          {
            label: "Net",
            type: "line",
            data: net,
            borderColor: "rgba(75, 192, 192, 1)",
            borderWidth: 2,
            tension: 0.25,
            fill: false,
          },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      resizeDelay: 150,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom" },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              return `${ctx.dataset.label}: ${formatCurrency(v)}`;
            },
          },
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          ticks: { precision: 0 },
        },
      },
    },
  });
}
