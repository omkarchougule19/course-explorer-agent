// calendar-page.js - page script for calendar.html, moved out of the HTML so the CSP can
// forbid inline scripts (script-src 'self'). Loaded at the same spot
// the inline block was, as a classic synchronous script, so execution
// order and globals are unchanged.
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const CURRENT_TERM = 'fall 2026';  // keep in sync with app/terms.py

  async function getJSON(url) {
    const res = await fetch(url);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
    return body;
  }

  function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'T00:00:00');
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
  }

  function renderEvents(events) {
    const wrap = $('cal-wrap');
    if (!events.length) {
      wrap.innerHTML = '<p class="empty-state">No calendar events loaded for this term yet.</p>';
      return;
    }
    const byDate = new Map();
    for (const ev of events) {
      const key = ev.event_date || ev.raw_date || 'Undated';
      if (!byDate.has(key)) byDate.set(key, []);
      byDate.get(key).push(ev);
    }
    let html = '';
    for (const [date, evs] of byDate) {
      html += `<div class="cal-day"><h3>${esc(formatDate(date) || date)}</h3><div class="cal-events">`;
      for (const ev of evs) {
        html += `<div class="cal-event"><span class="cal-cat">${esc(ev.category || '')}</span>`
          + `<span class="cal-title">${esc(ev.title || '')}</span></div>`;
      }
      html += '</div></div>';
    }
    const [year, semester] = ($('cal-term').value || '').split('|');
    html += Citations.line(Citations.link(Citations.registrarCalendarUrl(year, semester), 'UIUC Registrar Academic Calendar'));
    wrap.innerHTML = html;
  }

  async function loadCalendar() {
    const wrap = $('cal-wrap');
    wrap.setAttribute('aria-busy', 'true');
    const [year, semester] = $('cal-term').value.split('|');
    const category = $('cal-category').value;
    const params = new URLSearchParams();
    if (year) params.set('year', year);
    if (semester) params.set('semester', semester);
    if (category) params.set('category', category);
    try {
      const data = await getJSON('/calendar?' + params.toString());
      renderEvents(data.events || []);
    } catch (e) {
      wrap.innerHTML = `<p class="empty-state">Couldn't load the calendar: ${esc(e.message)}</p>`;
    }
  }

  async function loadTerms() {
    try {
      const stats = await getJSON('/stats');
      const select = $('cal-term');
      const order = { spring: 0, summer: 1, fall: 2 };
      const terms = (stats.terms_covered || []).slice().sort((a, b) => {
        const [sa, ya] = a.split(' '), [sb, yb] = b.split(' ');
        return (yb - ya) || (order[sb] - order[sa]);
      });
      for (const t of terms) {
        const [sem, yr] = t.split(' ');
        const opt = document.createElement('option');
        opt.value = `${yr}|${sem}`;
        opt.textContent = t;
        if (t === CURRENT_TERM) opt.selected = true;
        select.appendChild(opt);
      }
    } catch (e) { /* leaves the select empty; loadCalendar still runs with no filter */ }
  }

  $('cal-term').addEventListener('change', loadCalendar);
  $('cal-category').addEventListener('change', loadCalendar);

  (async () => {
    await loadTerms();
    loadCalendar();
  })();
