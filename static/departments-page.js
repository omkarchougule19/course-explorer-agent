// departments-page.js - page script for departments.html, moved out of the HTML so the CSP can
// forbid inline scripts (script-src 'self'). Loaded at the same spot
// the inline block was, as a classic synchronous script, so execution
// order and globals are unchanged.
  const $ = (id) => document.getElementById(id);

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const REFRESH_ICON =
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">' +
    '<path d="M21 12a9 9 0 0 0-9-9 9 9 0 0 0-6.7 3L3 8"/><path d="M3 3v5h5"/>' +
    '<path d="M3 12a9 9 0 0 0 9 9 9 9 0 0 0 6.7-3L21 16"/><path d="M21 21v-5h-5"/></svg>';

  const SEVEN_DAYS = 7 * 86400000;
  let deptRows = [];
  let selected = null;

  // Rank: most pending sync requests first, then stalest (oldest / never
  // synced), then alphabetical - so the top of the list is what actually
  // needs attention.
  function deptRank(a, b) {
    const pa = Number(a.pending_count) || 0, pb = Number(b.pending_count) || 0;
    if (pa !== pb) return pb - pa;
    const sa = a.last_synced_at ? parseUtc(a.last_synced_at).getTime() : 0;
    const sb = b.last_synced_at ? parseUtc(b.last_synced_at).getTime() : 0;
    if (sa !== sb) return sa - sb;
    return a.subject.localeCompare(b.subject);
  }

  function renderList() {
    const wrap = $('dept-list');
    const q = $('dept-filter').value.trim().toUpperCase();
    const filtered = (q ? deptRows.filter(r => r.subject.includes(q)) : deptRows.slice()).sort(deptRank);
    if (!filtered.length) {
      wrap.innerHTML = '<p class="empty-state">No departments match.</p>';
      return;
    }
    if (!selected || !filtered.some(r => r.subject === selected)) selected = filtered[0].subject;
    wrap.innerHTML = filtered.map(r => {
      const active = r.subject === selected ? ' active' : '';
      const pending = Number(r.pending_count) || 0;
      const badge = pending ? `<span class="dept-badge">${pending}</span>` : '';
      return `<button type="button" class="${active.trim()}" data-subject="${esc(r.subject)}">${esc(r.subject)}${badge}</button>`;
    }).join('');
    renderDetail();
  }

  function renderDetail() {
    const wrap = $('dept-detail');
    const row = deptRows.find(r => r.subject === selected);
    if (!row) {
      wrap.innerHTML = '<p class="empty-state">Choose a subject from the list.</p>';
      return;
    }
    const synced = row.last_synced_at;
    const fresh = synced && (Date.now() - parseUtc(synced).getTime()) < SEVEN_DAYS;
    const when = synced ? formatRelativeTime(synced) : 'never';
    const action = fresh
      ? '<span class="pill pill-open">current</span>'
      : `<button type="button" id="dept-sync-btn" data-subject="${esc(row.subject)}">${REFRESH_ICON}Sync</button>`;
    wrap.innerHTML = `
      <h2 class="dept-detail-title">${esc(row.subject)}</h2>
      <dl class="dept-detail-stats">
        <div><dt>Sections</dt><dd>${Number(row.section_count) || 0}</dd></div>
        <div><dt>Last Synced</dt><dd>${esc(when)}</dd></div>
        <div><dt>Pending Requests</dt><dd>${Number(row.pending_count) || 0}</dd></div>
      </dl>
      <p class="hint">${fresh ? 'This department was synced within the last week — no refresh needed.' : 'This department hasn’t synced recently.'}</p>
      <div style="margin-top: var(--s3);">${action}</div>
    `;
    $('dept-courses-subject').textContent = row.subject;
    $('dept-courses-panel').hidden = false;
    loadDeptCourses();
  }

  // ---- Courses in the selected department --------------------------------
  // Same filters, same Add-to-schedule buttons, same quick view as Browse
  // Sections on the homepage - both pages share course-results.js so this
  // is one implementation, not two that can drift apart.
  const deptCourseResults = CourseResults.create({
    wrapEl: $('dc-results'),
    containerEl: $('dept-courses-panel'),
    courseDetailEl: $('dc-course-detail'),
    syncUrl: false,
    emptyMessage: 'No courses match those filters in this department.',
  });

  async function getJSON(url) {
    const res = await fetch(url);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
    return body;
  }

  function showSkeleton(wrap, rows = 6) {
    wrap.setAttribute('aria-busy', 'true');
    wrap.innerHTML = '<div class="skeleton">'
      + Array.from({ length: rows }, () => '<div class="sk-line"></div>').join('')
      + '</div>';
  }

  async function loadDeptCourses() {
    if (!selected) return;
    const params = new URLSearchParams();
    params.set('subject', selected);
    const term = $('dc-term').value.trim();
    const level = $('dc-level').value.trim();
    const course = $('dc-course').value.trim();
    const instructor = $('dc-instructor').value.trim();
    const limit = $('dc-limit').value.trim();
    if (term) { const [y, s] = term.split('|'); params.set('year', y); params.set('semester', s); }
    if (level) params.set('level', level);
    if (course) params.set('course_number', course);
    if (instructor) params.set('instructor', instructor);
    if (limit) params.set('limit', limit);

    showSkeleton($('dc-results'));
    try {
      const rows = await getJSON('/sections?' + params.toString());
      if (!rows.length) {
        $('dc-results').removeAttribute('aria-busy');
        $('dc-results').innerHTML = `<p class="empty-state">${esc(emptyDeptMessage())}</p>`;
        $('dc-course-detail').hidden = true;
        document.querySelector('.row-hint').hidden = true;
        return;
      }
      deptCourseResults.render(rows);
    } catch (err) {
      $('dc-results').removeAttribute('aria-busy');
      $('dc-results').innerHTML = `<p class="empty-state">Query failed: ${esc(err.message)}</p>`;
      document.querySelector('.row-hint').hidden = true;
    }
  }

  // Distinguishes "this department just hasn't been synced for the
  // selected term" (the common case - see the demand-driven refresh model
  // in DECISIONS.md, sync is manual and per-department) from "no filters
  // match" or "nothing on file at all yet", instead of one generic message
  // that reads like a bug either way.
  function emptyDeptMessage() {
    const row = deptRows.find(r => r.subject === selected);
    const termSelect = $('dc-term');
    const termOpt = termSelect.selectedOptions[0];
    const termLabel = termSelect.value ? termOpt.textContent : null;
    const hasAnyData = row && Number(row.section_count) > 0;
    const otherFiltersSet = $('dc-level').value || $('dc-course').value.trim() || $('dc-instructor').value.trim();

    if (otherFiltersSet && hasAnyData) {
      return 'No courses match those filters in this department. Try clearing the course #, instructor, or level.';
    }
    if (!hasAnyData) {
      return 'Not synced yet — no course data on file for this department. Hit Sync above to request it.';
    }
    if (termLabel) {
      return `Not synced for ${termLabel} — this department's last sync didn't cover that term. Hit Sync above, or switch to "All terms" to see what's on file.`;
    }
    return 'No courses match those filters in this department.';
  }

  $('dept-course-form').addEventListener('submit', (e) => {
    e.preventDefault();
    loadDeptCourses();
  });

  const DC_CURRENT_TERM = 'fall 2026';  // keep in sync with app/terms.py

  async function loadDeptTerms() {
    try {
      const stats = await getJSON('/stats');
      const select = $('dc-term');
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
        if (t === DC_CURRENT_TERM) opt.selected = true;
        select.appendChild(opt);
      }
    } catch (e) { /* stays "All terms" */ }
  }

  async function loadDepartments() {
    try {
      const res = await fetch('/sync/status');
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
      deptRows = body.departments || [];
      renderList();
    } catch (e) {
      $('dept-list').innerHTML = `<p class="empty-state">Couldn't load department data: ${esc(e.message)}</p>`;
    }
  }

  $('dept-filter').addEventListener('input', renderList);

  $('dept-list').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-subject]');
    if (!btn) return;
    selected = btn.dataset.subject;
    renderList();
  });

  $('dept-detail').addEventListener('click', async (e) => {
    const btn = e.target.closest('#dept-sync-btn');
    if (!btn) return;
    const subject = btn.dataset.subject;
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const res = await fetch('/sync/request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
      const row = deptRows.find(r => r.subject === subject);
      if (row) row.pending_count = body.pending_count;
      renderDetail();
      renderList();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = 'Retry';
    }
  });

  // loadDeptTerms() must finish first: it pre-selects "fall 2026" in the
  // term dropdown, and loadDepartments() triggers the first course fetch
  // (via renderDetail() auto-selecting a department) - if that fetch ran
  // before the dropdown had a value, it would query with no term filter
  // while the dropdown still visibly showed "fall 2026" selected.
  (async () => {
    await loadDeptTerms();
    loadDepartments();
  })();
