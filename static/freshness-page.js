// freshness-page.js - page script for freshness.html, moved out of the HTML so the CSP can
// forbid inline scripts (script-src 'self'). Loaded at the same spot
// the inline block was, as a classic synchronous script, so execution
// order and globals are unchanged.
  const $ = (id) => document.getElementById(id);

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  async function getJSON(url) {
    const res = await fetch(url);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(body.detail || `Request failed (${res.status})`);
      throw err;
    }
    return body;
  }

  function ageInDays(sqliteTimestamp) {
    return (Date.now() - parseUtc(sqliteTimestamp).getTime()) / 86400000;
  }

  function ageClass(sqliteTimestamp) {
    const days = ageInDays(sqliteTimestamp);
    if (days < 35) return 'fresh';
    if (days > 70) return 'stale';
    return '';
  }

  function renderFreshness(rows) {
    const wrap = $('freshness-wrap');
    if (!rows.length) {
      wrap.innerHTML = '<p class="empty-state">No data yet. Run the scraper, then reload this page.</p>';
      return;
    }
    let html = '<table><thead><tr>'
      + '<th>Subject</th><th>Term</th><th>Sections</th><th>Last Updated</th>'
      + '</tr></thead><tbody>';
    for (const row of rows) {
      const cls = ageClass(row.last_updated);
      const abs = row.last_updated ? parseUtc(row.last_updated).toLocaleString() : '—';
      html += `<tr>`
        + `<td>${esc(row.subject)}</td>`
        + `<td>${esc(row.semester)} ${Number(row.year) || ''}</td>`
        + `<td>${Number(row.section_count) || 0}</td>`
        + `<td class="${cls}" title="${esc(abs)}">${esc(formatRelativeTime(row.last_updated))}</td>`
        + `</tr>`;
    }
    html += '</tbody></table>';
    html += Citations.line(Citations.link(Citations.courseExplorerUrl(), 'UIUC Course Explorer'));
    wrap.innerHTML = html;
  }

  async function loadFreshness() {
    try {
      const data = await getJSON('/freshness');
      renderFreshness(data.freshness || []);
    } catch (e) {
      $('freshness-wrap').innerHTML = `<p class="empty-state">Couldn't load freshness data: ${e.message}</p>`;
    }
  }

  loadFreshness();
