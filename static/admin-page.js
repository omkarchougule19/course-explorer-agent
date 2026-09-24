// admin-page.js - page script for admin.html, moved out of the HTML so the CSP can
// forbid inline scripts (script-src 'self'). Loaded at the same spot
// the inline block was, as a classic synchronous script, so execution
// order and globals are unchanged.
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const TOKEN_KEY = 'adminToken';
  const getToken = () => { try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } };
  const setToken = (t) => { try { sessionStorage.setItem(TOKEN_KEY, t); } catch (e) {} };
  const clearToken = () => { try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {} };

  class AuthError extends Error {}

  // One helper for every admin call. A 403 becomes an AuthError so callers
  // can drop back to the token gate; any other non-2xx throws with the
  // server's detail message.
  async function adminReq(path, method = 'GET') {
    const res = await fetch(path, { method, headers: { 'X-Admin-Token': getToken() } });
    if (res.status === 403) throw new AuthError('forbidden');
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
    return body;
  }
  const adminFetch = (path) => adminReq(path);
  const adminPost = (path) => adminReq(path, 'POST');

  function showGate(rejected) {
    $('dash').hidden = true;
    $('lock').hidden = true;
    $('gate').hidden = false;
    $('gate-err').hidden = !rejected;
    $('gate-token').focus();
  }
  function showDash() {
    $('gate').hidden = true;
    $('dash').hidden = false;
    $('lock').hidden = false;
    loadAll();
  }

  // A panel loader wrapper: renders into `wrap`, routes 403 to the gate,
  // shows any other failure inline so one dead panel never blanks the page.
  async function panel(wrapId, fn) {
    const wrap = $(wrapId);
    try {
      await fn(wrap);
    } catch (e) {
      if (e instanceof AuthError) { clearToken(); showGate(true); return; }
      wrap.innerHTML = `<p class="empty-state">Couldn't load: ${esc(e.message)}</p>`;
    }
  }

  const fmtTs = (ts) => ts ? esc(String(ts).replace('T', ' ').replace('+00:00', 'Z')) : '—';

  // ask_log / answer_feedback store ts as ISO-8601 with a +00:00 offset,
  // which `new Date()` parses natively - unlike time.js's parseUtc(), which
  // is built for SQLite's offset-less CURRENT_TIMESTAMP and would yield NaN.
  function relTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return esc(String(ts));
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return 'just now';
    const m = Math.round(s / 60); if (m < 60) return m + 'm ago';
    const h = Math.round(m / 60); if (h < 24) return h + 'h ago';
    const dd = Math.round(h / 24); if (dd < 30) return dd + 'd ago';
    const mo = Math.round(dd / 30); if (mo < 12) return mo + 'mo ago';
    return Math.round(mo / 12) + 'y ago';
  }

  async function loadTiles() {
    try {
      const { summary: s } = await adminFetch('/admin/ask-stats');
      $('t-u24').textContent = s.unique_24h;
      $('t-u7').textContent = s.unique_7d;
      $('t-uall').textContent = s.unique_all;
      $('t-q24').textContent = s.questions_24h;
      const fb = s.feedback || { up: 0, down: 0, down_unreviewed: 0 };
      $('t-dvr').textContent = fb.down_unreviewed;
      $('t-fb').textContent = `${fb.up} up · ${fb.down} down total`;

      const sf = s.site_feedback || { total: 0, unreviewed: 0 };
      $('t-sfr').textContent = sf.unreviewed;
      $('t-sf').textContent = `${sf.total} total`;

      const order = ['answered', 'refused', 'pending', 'rate_limited', 'global_limited', 'too_long', 'error'];
      const a = s.outcomes_24h || {}, b = s.outcomes_all || {};
      $('outcomes').innerHTML = order.map((k) =>
        `<div><dt>${k} <span style="opacity:.6">(24h)</span></dt><dd>${a[k] || 0}</dd></div>` +
        `<div><dt>${k} <span style="opacity:.6">(all)</span></dt><dd>${b[k] || 0}</dd></div>`
      ).join('');
    } catch (e) {
      if (e instanceof AuthError) { clearToken(); showGate(true); return; }
      $('outcomes').innerHTML = `<div><dt>Couldn't load: ${esc(e.message)}</dt><dd></dd></div>`;
    }
  }

  function loadActivity() {
    return panel('chart', async (wrap) => {
      const { daily } = await adminFetch('/admin/activity?days=30');
      if (!daily || !daily.length) { wrap.innerHTML = '<p class="empty-state">No activity yet.</p>'; return; }
      const W = 900, H = 180, padB = 22, padL = 6;
      const max = Math.max(...daily.map((d) => d.questions), 1);
      const bw = (W - padL * 2) / daily.length;
      let bars = '';
      daily.forEach((d, i) => {
        const h = Math.round((H - padB) * (d.questions / max));
        const x = padL + i * bw;
        const y = H - padB - h;
        bars += `<rect class="bar" x="${(x + bw * 0.15).toFixed(1)}" y="${y}" `
          + `width="${(bw * 0.7).toFixed(1)}" height="${h}"><title>${esc(d.day)}: ${d.questions} questions, ${d.uniq} unique</title></rect>`;
        if (i % 5 === 0) bars += `<text class="tick" x="${(x + bw / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(d.day.slice(5))}</text>`;
      });
      wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="questions per day">`
        + `<line class="axis" x1="${padL}" y1="${H - padB}" x2="${W - padL}" y2="${H - padB}"/>${bars}</svg>`;
    });
  }

  function loadClients() {
    return panel('clients-wrap', async (wrap) => {
      const { clients } = await adminFetch('/admin/clients?limit=100');
      if (!clients || !clients.length) { wrap.innerHTML = '<p class="empty-state">No clients yet.</p>'; return; }
      let h = '<table><thead><tr><th>Client IP</th><th>Questions</th><th>Model calls</th><th>First seen</th><th>Last seen</th></tr></thead><tbody>';
      for (const c of clients) {
        h += `<tr><td class="crn">${esc(c.client_ip)}</td>`
          + `<td class="num">${c.questions}</td><td class="num">${c.llm_calls}</td>`
          + `<td title="${fmtTs(c.first_seen)}">${relTime(c.first_seen)}</td>`
          + `<td title="${fmtTs(c.last_seen)}">${relTime(c.last_seen)}</td></tr>`;
      }
      wrap.innerHTML = h + '</tbody></table>';
    });
  }

  function outcomePill(o) {
    const warn = ['rate_limited', 'global_limited', 'too_long'];
    const cls = o === 'answered' ? 'pill-open' : o === 'refused' ? 'pill-closed'
      : o === 'error' ? 'pill-err' : warn.includes(o) ? 'pill-warn' : '';
    return cls ? `<span class="pill ${cls}">${esc(o)}</span>` : esc(o);
  }

  function loadHistory() {
    return panel('history-wrap', async (wrap) => {
      const p = new URLSearchParams();
      const oc = $('f-outcome').value.trim();
      const ip = $('f-ip').value.trim();
      const lim = $('f-limit').value.trim();
      if (oc) p.set('outcome', oc);
      if (ip) p.set('ip', ip);
      if (lim) p.set('limit', lim);
      const { entries } = await adminFetch('/admin/ask-log?' + p.toString());
      if (!entries || !entries.length) { wrap.innerHTML = '<p class="empty-state">No matching rows.</p>'; return; }
      let h = '<table><thead><tr><th>Time</th><th>IP</th><th>Outcome</th><th>ms</th><th>Question</th><th>Answer preview</th></tr></thead><tbody>';
      for (const e of entries) {
        h += `<tr><td title="${fmtTs(e.ts)}">${relTime(e.ts)}</td>`
          + `<td class="crn">${esc(e.client_ip ?? '—')}</td>`
          + `<td>${outcomePill(e.outcome)}</td>`
          + `<td class="num">${e.latency_ms ?? '—'}</td>`
          + `<td><span class="cell-clip">${esc(e.question)}</span></td>`
          + `<td><span class="cell-clip">${esc(e.answer_preview ?? '')}</span></td></tr>`;
      }
      wrap.innerHTML = h + '</tbody></table>';
      wrap.querySelectorAll('.cell-clip').forEach((el) =>
        el.addEventListener('click', () => el.classList.toggle('open')));
    });
  }

  function renderHistoryJson(raw) {
    if (!raw) return '';
    let turns;
    try { turns = JSON.parse(raw); } catch (e) { return `<div class="hist-turn">${esc(raw)}</div>`; }
    return turns.map((t) =>
      `<div class="hist-turn"><b>Q:</b> ${esc(t.q)}<br><b>A:</b> ${esc(t.a)}</div>`).join('');
  }

  function loadReview() {
    return panel('review-wrap', async (wrap) => {
      const showRev = $('show-reviewed').checked;
      const q = showRev ? '/admin/feedback?vote=down' : '/admin/feedback?vote=down&reviewed=0';
      const { feedback } = await adminFetch(q);
      if (!feedback || !feedback.length) {
        wrap.innerHTML = `<p class="empty-state">${showRev ? 'No downvotes recorded.' : 'Nothing to review. 🎉'}</p>`;
        return;
      }
      let h = '<table><thead><tr><th>Time</th><th>IP</th><th>Question</th><th>Answer</th><th>History</th><th></th></tr></thead><tbody>';
      for (const f of feedback) {
        const reviewed = !!f.reviewed_at;
        h += `<tr class="${reviewed ? 'reviewed' : ''}" data-id="${f.id}">`
          + `<td title="${fmtTs(f.ts)}">${relTime(f.ts)}</td>`
          + `<td class="crn">${esc(f.client_ip ?? '—')}</td>`
          + `<td><span class="cell-clip">${esc(f.question)}</span></td>`
          + `<td><span class="cell-clip">${esc(f.answer)}</span></td>`
          + `<td><span class="cell-clip">${f.history_json ? 'view transcript' : '—'}</span>`
          + `<div class="hist-full" hidden>${renderHistoryJson(f.history_json)}</div></td>`
          + `<td>${reviewed ? '<span class="pill pill-open">reviewed</span>'
            : `<button type="button" class="mark-btn">Mark reviewed</button>`}</td></tr>`;
      }
      wrap.innerHTML = h + '</tbody></table>';
      wrap.querySelectorAll('.cell-clip').forEach((el) => el.addEventListener('click', () => {
        el.classList.toggle('open');
        const full = el.parentElement.querySelector('.hist-full');
        if (full) full.hidden = !full.hidden;
      }));
      wrap.querySelectorAll('.mark-btn').forEach((btn) => btn.addEventListener('click', async () => {
        const tr = btn.closest('tr');
        btn.disabled = true; btn.textContent = '…';
        try {
          await adminPost(`/admin/feedback/${tr.dataset.id}/reviewed`);
          if ($('show-reviewed').checked) {
            tr.classList.add('reviewed');
            btn.replaceWith(Object.assign(document.createElement('span'), { className: 'pill pill-open', textContent: 'reviewed' }));
          } else {
            tr.remove();
            if (!$('review-wrap').querySelector('tbody tr')) loadReview();
          }
          loadTiles();
        } catch (e) {
          if (e instanceof AuthError) { clearToken(); showGate(true); return; }
          btn.disabled = false; btn.textContent = 'Retry';
        }
      }));
    });
  }

  function loadSiteFeedback() {
    return panel('site-feedback-wrap', async (wrap) => {
      const showRev = $('sf-show-reviewed').checked;
      const q = showRev ? '/admin/site-feedback' : '/admin/site-feedback?reviewed=0';
      const { feedback } = await adminFetch(q);
      if (!feedback || !feedback.length) {
        wrap.innerHTML = `<p class="empty-state">${showRev ? 'No site feedback yet.' : 'Nothing to review. 🎉'}</p>`;
        return;
      }
      let h = '<table><thead><tr><th>Time</th><th>IP</th><th>Page</th><th>Message</th><th></th></tr></thead><tbody>';
      for (const f of feedback) {
        const reviewed = !!f.reviewed_at;
        h += `<tr class="${reviewed ? 'reviewed' : ''}" data-id="${f.id}">`
          + `<td title="${fmtTs(f.ts)}">${relTime(f.ts)}</td>`
          + `<td class="crn">${esc(f.client_ip ?? '—')}</td>`
          + `<td>${esc(f.page ?? '—')}</td>`
          + `<td><span class="cell-clip">${esc(f.message)}</span></td>`
          + `<td>${reviewed ? '<span class="pill pill-open">reviewed</span>'
            : `<button type="button" class="mark-btn">Mark reviewed</button>`}</td></tr>`;
      }
      wrap.innerHTML = h + '</tbody></table>';
      wrap.querySelectorAll('.cell-clip').forEach((el) =>
        el.addEventListener('click', () => el.classList.toggle('open')));
      wrap.querySelectorAll('.mark-btn').forEach((btn) => btn.addEventListener('click', async () => {
        const tr = btn.closest('tr');
        btn.disabled = true; btn.textContent = '…';
        try {
          await adminPost(`/admin/site-feedback/${tr.dataset.id}/reviewed`);
          if ($('sf-show-reviewed').checked) {
            tr.classList.add('reviewed');
            btn.replaceWith(Object.assign(document.createElement('span'), { className: 'pill pill-open', textContent: 'reviewed' }));
          } else {
            tr.remove();
            if (!$('site-feedback-wrap').querySelector('tbody tr')) loadSiteFeedback();
          }
          loadTiles();
        } catch (e) {
          if (e instanceof AuthError) { clearToken(); showGate(true); return; }
          btn.disabled = false; btn.textContent = 'Retry';
        }
      }));
    });
  }

  function loadAll() {
    loadTiles();
    loadActivity();
    loadClients();
    loadHistory();
    loadReview();
    loadSiteFeedback();
  }

  $('gate-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = $('gate-token').value.trim();
    if (!t) return;
    setToken(t);
    $('gate-token').value = '';
    showDash();
  });
  $('lock').addEventListener('click', () => { clearToken(); location.reload(); });
  $('f-run').addEventListener('click', loadHistory);
  $('f-outcome').addEventListener('change', loadHistory);
  $('show-reviewed').addEventListener('change', loadReview);
  $('sf-show-reviewed').addEventListener('change', loadSiteFeedback);

  if (getToken()) showDash(); else showGate(false);
