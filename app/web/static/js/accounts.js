// app/web/static/js/accounts.js
// ─────────────────────────────────────────────────────────────
// Accounts Page Logic
// Handles: Manual account creation, manual value addition,
// and view toggle between "Overview" and "Manual Asset Management"
// ─────────────────────────────────────────────────────────────

// Dynamically load available account classes based on side
function loadClasses() {
  const sideSel = document.getElementById('account_side');
  const classSel = document.getElementById('account_class');
  if (!sideSel || !classSel) return;

  classSel.innerHTML = '';
  const classes = (window.CLASSES_BY_SIDE || {})[sideSel.value] || [];
  classes.forEach(c => {
    const o = document.createElement('option');
    o.value = c;
    o.textContent = c.replace(/_/g, ' ').toUpperCase();
    classSel.appendChild(o);
  });
}

// Post JSON utility
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  return r.json();
}

// Manual account creation
function initAccountCreateForm() {
  const form = document.getElementById('acctCreateForm');
  const status = document.getElementById('createStatus');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = Object.fromEntries(fd.entries());
    const res = await postJSON('/api/accounts/create_manual', payload);
    status.textContent = res.ok ? `created: ${res.account_id}` : (res.error || 'error');
    if (res.ok) setTimeout(() => location.reload(), 800);
  });
}

// Manual amount addition
function initManualValueForm() {
  const form = document.getElementById('valueForm');
  const status = document.getElementById('valueStatus');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = Object.fromEntries(fd.entries());
    payload.amount = parseFloat(payload.amount);
    payload.date = payload.operation_date; // backward compat
    const res = await postJSON('/api/accounts/add_manual_value', payload);
    status.textContent = res.ok ? 'saved' : (res.error || 'error');
  });
}

// Toggle between Overview and Manual modes
function initViewToggle() {
  const btnOverview = document.getElementById('btnOverview');
  const btnManual = document.getElementById('btnManual');
  const overviewSection = document.getElementById('overviewSection');
  const manualSection = document.getElementById('manualSection');

  if (!btnOverview || !btnManual) return;

  function activateOverview() {
    btnOverview.classList.add('btn-primary', 'active');
    btnOverview.classList.remove('btn-outline-primary');
    btnManual.classList.add('btn-outline-secondary');
    btnManual.classList.remove('btn-secondary', 'active');
    overviewSection.style.display = 'block';
    manualSection.style.display = 'none';
  }

  function activateManual() {
    btnManual.classList.add('btn-secondary', 'active');
    btnManual.classList.remove('btn-outline-secondary');
    btnOverview.classList.add('btn-outline-primary');
    btnOverview.classList.remove('btn-primary', 'active');
    overviewSection.style.display = 'none';
    manualSection.style.display = 'block';
  }

  btnOverview.addEventListener('click', activateOverview);
  btnManual.addEventListener('click', activateManual);
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
  // Init dropdown linkage
  const sideSel = document.getElementById('account_side');
  if (sideSel) {
    sideSel.addEventListener('change', loadClasses);
    loadClasses();
  }

  // Init forms
  initAccountCreateForm();
  initManualValueForm();

  // Init toggle
  initViewToggle();
});
