const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
async function api(path, body) {
  let r;
  try {
    r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  } catch (e) { throw new Error('Cannot reach the server'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || 'Request failed'), {status: r.status});
  return j;
}
// Re-render el with html, keeping whatever the user was typing (inputs need stable ids).
function render(el, html) {
  if (el._html === html) return;
  el._html = html;
  const keep = {};
  el.querySelectorAll('input[id],textarea[id],select[id]').forEach(i => {
    keep[i.id] = {v: i.value, f: i === document.activeElement, s: i.selectionStart, e: i.selectionEnd};
  });
  el.innerHTML = html;
  for (const id in keep) {
    const i = document.getElementById(id);
    if (!i) continue;
    i.value = keep[id].v;
    if (keep[id].f) { i.focus(); try { i.setSelectionRange(keep[id].s, keep[id].e); } catch (e) {} }
  }
}
// Server-sent events with auto-reconnect (the browser retries on its own).
function listen(role, token, onState, onFail) {
  const es = new EventSource(`/events?role=${role}&token=${encodeURIComponent(token || '')}`);
  es.onmessage = m => { if ($('#conn')) $('#conn').hidden = true; onState(JSON.parse(m.data)); };
  es.onerror = () => {
    if ($('#conn')) $('#conn').hidden = false;
    if (es.readyState === EventSource.CLOSED) { es.close(); if (onFail) onFail(); }
  };
  return es;
}
const store = {
  get: k => { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} },
  del: k => { try { localStorage.removeItem(k); } catch (e) {} },
};
const fmt = n => Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
