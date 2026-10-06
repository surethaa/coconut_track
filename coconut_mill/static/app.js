// ---- Install as an app (PWA) ------------------------------------------------
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}
let deferredInstall = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  deferredInstall = e;
  document.querySelectorAll('[data-install]').forEach(b => { b.hidden = false; });
});
window.addEventListener('appinstalled', () => {
  document.querySelectorAll('[data-install]').forEach(b => { b.hidden = true; });
});
document.addEventListener('click', async ev => {
  const btn = ev.target.closest('[data-install]');
  if (btn && deferredInstall) {
    deferredInstall.prompt();
    await deferredInstall.userChoice;
    deferredInstall = null;
    btn.hidden = true;
  }
});

// ---- Confirm dialogs (no inline JS, so a strict CSP can stay on) -------------
document.addEventListener('submit', ev => {
  const msg = ev.target.dataset && ev.target.dataset.confirm;
  if (msg && !window.confirm(msg)) ev.preventDefault();
});

// ---- Live "total" preview in the entry form (server recalculates on save) ----
document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('form[data-key]');
  if (!form) return;
  const key = form.dataset.key;
  const v = n => parseFloat(form.elements[n] && form.elements[n].value) || 0;
  const calc = {
    coconut: () => v('qty') * v('unit_price'),
    copra: () => v('kg') * v('price'),
    shell: () => v('tons') * v('price'),
    expenses: () => v('labor') + v('transport'),
  };
  const out = document.getElementById('live-total');
  const update = () => {
    if (out && calc[key]) {
      out.value = calc[key]().toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    if (key === 'coconut') {
      const kg = form.elements.unit.value === 'kg';
      document.getElementById('lbl-qty').textContent = kg ? 'Quantity (kg)' : 'Quantity (count)';
      document.getElementById('lbl-unit_price').textContent = kg ? 'Unit price (per kg)' : 'Unit price (per coconut)';
    }
  };
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  update();
});
