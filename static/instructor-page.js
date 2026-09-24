// instructor-page.js - page script for instructor.html, moved out of the HTML so the CSP can
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
    if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
    return body;
  }

  const TERM_ORDER = { spring: 0, summer: 1, fall: 2 };
  function termLabel(year, semester) {
    return `${semester.charAt(0).toUpperCase()}${semester.slice(1)} ${year}`;
  }
  function termSort(a, b) {
    return (b.year - a.year) || (TERM_ORDER[b.semester] - TERM_ORDER[a.semester]);
  }

  function courseKeyOf(r) { return `${r.subject}|${r.course_number}`; }

  async function loadInstructor() {
    const name = new URLSearchParams(location.search).get('name');
    const panel = $('instructor-panel');
    if (!name) {
      panel.innerHTML = '<p class="empty-state">No instructor specified. Click an instructor’s name from Browse Sections or a course’s grade history to land here.</p>';
      return;
    }
    document.title = `${name} — Illini Course Copilot`;
    $('instructor-subtitle').textContent = `Courses taught by ${name}, and their grade history where available.`;

    let sections;
    try {
      sections = await getJSON(`/sections?instructor=${encodeURIComponent(name)}&limit=1000`);
    } catch (e) {
      panel.innerHTML = `<p class="empty-state">Couldn't load this instructor's sections: ${esc(e.message)}</p>`;
      return;
    }
    if (!sections.length) {
      panel.innerHTML = `<p class="empty-state">No sections found for an instructor matching "${esc(name)}".</p>`;
      return;
    }

    const courses = new Map();  // "SUBJ|123" -> { subject, course_number, course_label, terms: Set, sectionCount }
    for (const s of sections) {
      const key = courseKeyOf(s);
      if (!courses.has(key)) {
        courses.set(key, { subject: s.subject, course_number: s.course_number, course_label: s.course_label, terms: new Map(), sectionCount: 0 });
      }
      const c = courses.get(key);
      c.sectionCount++;
      const termKey = `${s.year}|${s.semester}`;
      c.terms.set(termKey, { year: s.year, semester: s.semester });
    }

    const rmpUrl = Citations.rmpSearchUrl(name);
    panel.innerHTML = `
      <h2>${esc(name)}${rmpUrl ? ` <a class="rmp-link" href="${rmpUrl}" target="_blank" rel="noopener">Ratings &rarr;</a>` : ''}</h2>
      ${rmpUrl ? '<p class="hint">Ratings on RateMyProfessors are self-selected by students who chose to leave one, and don\'t account for a course\'s difficulty or grading rigor — worth reading alongside the grade history below, not instead of it.</p>' : ''}
    `;
    const list = document.createElement('div');
    panel.appendChild(list);

    const sortedCourses = [...courses.values()].sort((a, b) =>
      a.subject.localeCompare(b.subject) || String(a.course_number).localeCompare(String(b.course_number))
    );

    for (const c of sortedCourses) {
      const block = document.createElement('div');
      block.className = 'instructor-course';
      const sortedTerms = [...c.terms.values()].sort(termSort);
      const terms = sortedTerms.map(t => termLabel(t.year, t.semester));
      const mostRecent = sortedTerms[0];
      const catalogUrl = Citations.courseExplorerUrl(mostRecent.year, mostRecent.semester, c.subject, c.course_number);
      block.innerHTML = `
        <h3>${esc(c.subject)} ${esc(c.course_number)}${c.course_label ? ' — ' + esc(c.course_label) : ''}</h3>
        <p class="hint">Taught: ${terms.map(esc).join(', ')} &middot; ${c.sectionCount} section${c.sectionCount === 1 ? '' : 's'} total</p>
        ${Citations.line(Citations.link(catalogUrl, 'UIUC Course Explorer'))}
        <div class="instructor-grades"><p class="empty-state">Loading grade history…</p></div>
      `;
      list.appendChild(block);

      // Best-effort, per course - a course with no grade_distributions rows
      // for this instructor 404s, which just means "nothing to show" here.
      getJSON(`/courses/${encodeURIComponent(c.subject)}/${encodeURIComponent(c.course_number)}/grade-trend?instructor=${encodeURIComponent(name)}`)
        .then((data) => {
          const rows = data.trend || [];
          if (!rows.length) { block.querySelector('.instructor-grades').innerHTML = ''; return; }
          let html = '<table><thead><tr><th>Term</th><th>Students</th><th>Avg GPA</th></tr></thead><tbody>';
          for (const t of rows) {
            html += `<tr><td>${esc(t.year_term ?? '—')}</td><td class="num">${esc(t.students ?? '—')}</td><td class="num">${t.average_gpa ?? '—'}</td></tr>`;
          }
          html += '</tbody></table>';
          html += Citations.line(Citations.link(Citations.GRADES_URL, 'wadefagen/datasets'));
          block.querySelector('.instructor-grades').innerHTML = html;
        })
        .catch(() => { block.querySelector('.instructor-grades').innerHTML = ''; });
    }
  }

  loadInstructor();
