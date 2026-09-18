// /static/js/dashboard.js
// Enhanced for 3 dashboard modes: Net Worth, Spending (Consumption), Cash Flow

// ─────────────────────────────────────────────
// 🔸 View Switching
// ─────────────────────────────────────────────
function showDashboardView(viewName) {
  document.querySelectorAll(".dashboard-view").forEach(div => div.classList.add("d-none"));
  const target = document.getElementById(`view-${viewName}`);
  if (target) target.classList.remove("d-none");

  // 🔹 Flash message for "Spending Only"
  const banner = document.getElementById("spendingInfoBanner");
  if (banner) {
    if (viewName === "spending") {
      banner.style.display = "block";
      banner.style.opacity = "1";

      // Automatically fade and hide after 4 seconds
      setTimeout(() => {
        banner.style.opacity = "0";
        setTimeout(() => {
          banner.style.display = "none";
        }, 600); // match CSS transition
      }, 4000);
    } else {
      banner.style.display = "none";
    }
  }

  const year = document.getElementById("yearSelect").value;
  const view = getViewFromURL();

  if (viewName === "networth") {
    renderNetWorthChart(year);
  } else if (viewName === "spending") {
    renderMonthlyChart(year);
    renderCategoryChart(view, year);
    renderStackedSpendingChart(year);
  } else if (viewName === "incomeexpense") {
    renderIncomeExpenseChart(year);
  }
}

// ─────────────────────────────────────────────
// 🔹 Helpers for label & URL
// ─────────────────────────────────────────────
function pluralize(word) {
  if (word === "category") return "categories";
  if (word === "tag") return "tags";
  return word + "s";
}

function setViewLabels(view) {
  const plural = pluralize(view);
  const viewLabel = document.getElementById("viewLabel");
  const tableLabel = document.getElementById("tableLabel");
  const colLabel = document.getElementById("colLabel");

  if (viewLabel) viewLabel.textContent = plural;
  if (tableLabel) tableLabel.textContent = plural;
  if (colLabel) colLabel.textContent = view.charAt(0).toUpperCase() + view.slice(1);
}

function syncURL(view) {
  const u = new URL(window.location.href);
  u.searchParams.set("view", view);
  history.replaceState(null, "", u.toString());
}

function getViewFromURL() {
  const params = new URLSearchParams(location.search);
  return params.get("view") === "tag" ? "tag" : "category";
}

// ─────────────────────────────────────────────
// 🔹 Initialization
// ─────────────────────────────────────────────
async function initDashboardJS() {
  const yearSelect = document.getElementById("yearSelect");
  const view = getViewFromURL();
  setViewLabels(view);

  // Default load → Net Worth
  showDashboardView("networth");

  // Year change listener
  yearSelect.addEventListener("change", async (e) => {
    const y = e.target.value;
    const activeBtn = document.querySelector("#dashboardModeGroup .btn.active");
    const activeView = activeBtn ? activeBtn.dataset.view : "networth";
    showDashboardView(activeView);
  });

  // Category/tag toggle
  document.querySelectorAll("input[name=viewToggle]").forEach(radio => {
    radio.addEventListener("change", async (e) => {
      const v = e.target.value;
      setViewLabels(v);
      syncURL(v);
      const activeBtn = document.querySelector("#dashboardModeGroup .btn.active");
      const activeView = activeBtn ? activeBtn.dataset.view : "networth";
      if (activeView === "spending") await renderCategoryChart(v, yearSelect.value);
    });
  });
}

document.addEventListener("DOMContentLoaded", initDashboardJS);
