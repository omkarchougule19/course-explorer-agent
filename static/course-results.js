// course-results.js — shared course-results table + quick-view, used by
// both index.html (Browse Sections) and departments.html (Courses in
// <subject>). One implementation so "same filters, same Add buttons, same
// quick-view" is actually true by construction, not just by copy-paste
// that can drift. Each page creates its own instance via
// CourseResults.create({...}), scoped to its own DOM elements, so two
// instances on the same page (not currently done, but supported) never
// collide.
(function (global) {
  'use strict';

  var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  async function getJSON(url) {
    var res = await fetch(url);
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      var err = new Error(body.detail || ('Request failed (' + res.status + ')'));
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function courseKey(subject, course) { return subject + '|' + course; }

  // Per /prereqs: groups are AND-ed together (each rendered as its own
  // line); the options *within* one group are always alternatives, joined
  // "or".
  function reqGroupText(group) {
    if (group.options && group.options.length) {
      return group.options.map(function (o) { return esc(o.subject) + ' ' + esc(o.course_number); }).join(' or ');
    }
    return (group.conditions || []).map(esc).join('; ');
  }

  function instructorLink(name) {
    return name
      ? '<a class="instructor-link" href="/instructor.html?name=' + encodeURIComponent(name) + '">' + esc(name) + '</a>'
      : '—';
  }

  /**
   * opts:
   *   wrapEl          - element the results table renders into
   *   containerEl     - ancestor element to attach the delegated click listener to
   *                      (must contain both wrapEl and courseDetailEl)
   *   courseDetailEl  - element the quick-view panel renders into
   *   syncUrl         - if true, keep ?course=SUBJ-NUM in the address bar while open
   *   emptyMessage    - shown when render(rows) gets an empty array
   */
  function create(opts) {
    var wrapEl = opts.wrapEl;
    var containerEl = opts.containerEl;
    var courseDetailEl = opts.courseDetailEl;
    var syncUrl = !!opts.syncUrl;
    var emptyMessage = opts.emptyMessage || 'No sections match those filters.';

    var courseCache = {};  // "SUBJ|123" -> { prereqs?: {...}, grades?: {...} }
    var openCourseKey = null;
    var openCourseDescription = '';
    var openTab = 'overview';

    function render(rows) {
      wrapEl.removeAttribute('aria-busy');
      courseDetailEl.hidden = true;
      openCourseKey = null;
      if (!rows.length) {
        wrapEl.innerHTML = '<p class="empty-state">' + esc(emptyMessage) + '</p>';
        return;
      }
      var labels = ['Subj', 'Course', 'Course Name', 'Section', 'CRN', 'Instructor', 'Status', 'Credit', ''];
      var html = '<table><thead><tr>' + labels.map(function (l) { return '<th>' + l + '</th>'; }).join('') + '</tr></thead><tbody>';
      rows.forEach(function (row) {
        var titleAttr = row.description ? ' title="' + esc(row.description) + '"' : '';
        var statusRaw = row.enrollment_status || '';
        var statusCell = '—';
        if (statusRaw) {
          var cls = /open/i.test(statusRaw) ? 'pill-open' : /closed/i.test(statusRaw) ? 'pill-closed' : '';
          statusCell = cls ? '<span class="pill ' + cls + '">' + esc(statusRaw) + '</span>' : esc(statusRaw);
        }
        var subj = esc(row.subject || '');
        var num = esc(row.course_number || '');
        var crn = esc(row.crn || '');
        var added = crn && global.ScheduleStore && global.ScheduleStore.has(row.crn);
        var addBtn = crn
          ? '<button type="button" class="sched-add-btn' + (added ? ' added' : '') + '" '
            + 'data-crn="' + crn + '" data-year="' + (row.year || '') + '" data-semester="' + esc(row.semester || '') + '" '
            + 'data-subject="' + subj + '" data-course="' + num + '" data-course-label="' + esc(row.course_label || '') + '" '
            + 'data-section="' + esc(row.section_name || '') + '" data-instructor="' + esc(row.instructor || '') + '" '
            + 'data-credit="' + esc(row.credit_hours || '') + '">' + (added ? 'Added' : 'Add') + '</button>'
          : '';
        html += '<tr class="course-row" data-subject="' + subj + '" data-course="' + num + '" data-description="' + esc(row.description || '') + '"' + titleAttr + '>'
          + '<td>' + (subj || '—') + '</td>'
          + '<td class="num">' + (num || '—') + '</td>'
          + '<td>' + esc(row.course_label || '—') + '</td>'
          + '<td>' + esc(row.section_name || '—') + '</td>'
          + '<td class="crn">' + (crn || '—') + '</td>'
          + '<td>' + instructorLink(row.instructor) + '</td>'
          + '<td>' + statusCell + '</td>'
          + '<td>' + esc(row.credit_hours || '—') + '</td>'
          + '<td>' + addBtn + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      wrapEl.innerHTML = html;
      var p = document.createElement('p');
      p.className = 'hint';
      p.style.margin = 'var(--s2) var(--s4) 0';
      p.textContent = 'Click a row for the full course view — description, prerequisites, and grade history.';
      wrapEl.appendChild(p);
    }

    async function renderCourseTab(subject, course, description) {
      var panel = courseDetailEl.querySelector('.course-tab-panel');
      if (!panel) return;
      var key = courseKey(subject, course);
      courseCache[key] = courseCache[key] || {};

      if (openTab === 'overview') {
        panel.innerHTML = description
          ? '<p>' + esc(description) + '</p>'
          : '<p class="empty-state">No catalog description on file for this course.</p>';
        return;
      }

      if (openTab === 'prereqs') {
        if (courseCache[key].prereqs === undefined) {
          panel.innerHTML = '<p class="empty-state">Loading…</p>';
          try {
            courseCache[key].prereqs = await getJSON('/courses/' + encodeURIComponent(subject) + '/' + encodeURIComponent(course) + '/prereqs');
          } catch (e) {
            courseCache[key].prereqs = null;
          }
        }
        var data = courseCache[key].prereqs;
        if (!data || !data.prerequisites || !data.prerequisites.length) {
          panel.innerHTML = '<p class="empty-state">No parsed prerequisite data for this course.</p>';
          return;
        }
        var html = data.raw ? '<p class="hint" style="margin-top:0;">' + esc(data.raw) + '</p>' : '';
        html += data.prerequisites.map(function (g) { return '<div class="req-group">' + reqGroupText(g) + '</div>'; }).join('');
        if (data.unlocks && data.unlocks.length) {
          html += '<p class="hint">Unlocks: ' + data.unlocks.map(function (u) { return esc(u.subject) + ' ' + esc(u.course_number); }).join(', ') + '</p>';
        }
        panel.innerHTML = html;
        return;
      }

      if (openTab === 'grades') {
        if (courseCache[key].grades === undefined) {
          panel.innerHTML = '<p class="empty-state">Loading…</p>';
          try {
            courseCache[key].grades = await getJSON('/courses/' + encodeURIComponent(subject) + '/' + encodeURIComponent(course) + '/grade-trend');
          } catch (e) {
            courseCache[key].grades = null;
          }
        }
        var gdata = courseCache[key].grades;
        if (!gdata || !gdata.trend || !gdata.trend.length) {
          panel.innerHTML = '<p class="empty-state">No grade distribution data for this course.</p>';
          return;
        }
        var ghtml = '<table><thead><tr><th>Term</th><th>Instructor</th><th>Students</th><th>Avg GPA</th></tr></thead><tbody>';
        gdata.trend.forEach(function (t) {
          ghtml += '<tr><td>' + esc(t.year_term || '—') + '</td><td>' + instructorLink(t.primary_instructor) + '</td>'
            + '<td class="num">' + esc(t.students != null ? t.students : '—') + '</td><td class="num">' + (t.average_gpa != null ? t.average_gpa : '—') + '</td></tr>';
        });
        ghtml += '</tbody></table>';
        panel.innerHTML = ghtml;
      }
    }

    function closeCourseDetail() {
      courseDetailEl.hidden = true;
      openCourseKey = null;
      wrapEl.querySelectorAll('tr.course-row.open').forEach(function (r) { r.classList.remove('open'); });
      if (syncUrl) history.replaceState(null, '', location.pathname);
    }

    function openCourseDetail(subject, course, description) {
      var key = courseKey(subject, course);
      if (openCourseKey === key && !courseDetailEl.hidden) {
        closeCourseDetail();
        return;
      }
      openCourseKey = key;
      openCourseDescription = description || '';
      openTab = 'overview';
      wrapEl.querySelectorAll('tr.course-row.open').forEach(function (r) { r.classList.remove('open'); });
      wrapEl.querySelectorAll('tr.course-row[data-subject="' + subject + '"][data-course="' + course + '"]')
        .forEach(function (r) { r.classList.add('open'); });
      if (syncUrl) history.replaceState(null, '', '?course=' + encodeURIComponent(subject) + '-' + encodeURIComponent(course));

      courseDetailEl.hidden = false;
      courseDetailEl.innerHTML =
        '<div class="course-detail-head">'
        + '<h3>' + esc(subject) + ' ' + esc(course) + '</h3>'
        + '<div class="course-detail-actions">'
        + (syncUrl ? '<button type="button" class="course-detail-share">Copy Link</button>' : '')
        + '<button type="button" class="course-detail-close">Close</button>'
        + '</div></div>'
        + '<div class="course-tabs">'
        + '<button type="button" class="course-tab active" data-tab="overview">Overview</button>'
        + '<button type="button" class="course-tab" data-tab="prereqs">Prerequisites</button>'
        + '<button type="button" class="course-tab" data-tab="grades">Grade History</button>'
        + '</div><div class="course-tab-panel"></div>';
      renderCourseTab(subject, course, description);
      courseDetailEl.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'nearest' });
    }

    containerEl.addEventListener('click', function (e) {
      if (e.target.closest('.instructor-link')) return;  // let the anchor navigate normally

      var addBtn = e.target.closest('.sched-add-btn');
      if (addBtn) {
        var crn = addBtn.dataset.crn;
        if (global.ScheduleStore.has(crn)) {
          global.ScheduleStore.remove(crn);
          addBtn.classList.remove('added');
          addBtn.textContent = 'Add';
        } else {
          global.ScheduleStore.add({
            year: Number(addBtn.dataset.year) || null,
            semester: addBtn.dataset.semester,
            subject: addBtn.dataset.subject,
            course_number: addBtn.dataset.course,
            course_label: addBtn.dataset.courseLabel,
            section_name: addBtn.dataset.section,
            crn: crn,
            instructor: addBtn.dataset.instructor,
            credit_hours: addBtn.dataset.credit,
          });
          addBtn.classList.add('added');
          addBtn.textContent = 'Added';
        }
        return;
      }

      if (e.target.closest('.course-detail-close')) { closeCourseDetail(); return; }

      var shareBtn = e.target.closest('.course-detail-share');
      if (shareBtn) {
        navigator.clipboard.writeText(location.href).then(function () {
          var original = shareBtn.textContent;
          shareBtn.textContent = 'Copied!';
          setTimeout(function () { shareBtn.textContent = original; }, 1500);
        }).catch(function () { /* clipboard unavailable - link is still in the address bar */ });
        return;
      }

      var tabBtn = e.target.closest('.course-tab');
      if (tabBtn) {
        openTab = tabBtn.dataset.tab;
        courseDetailEl.querySelectorAll('.course-tab').forEach(function (b) { b.classList.toggle('active', b === tabBtn); });
        if (openCourseKey) {
          var parts = openCourseKey.split('|');
          renderCourseTab(parts[0], parts[1], openCourseDescription);
        }
        return;
      }

      var row = e.target.closest('tr.course-row');
      if (row) openCourseDetail(row.dataset.subject, row.dataset.course, row.dataset.description);
    });

    return { render: render, openCourseDetail: openCourseDetail };
  }

  global.CourseResults = { create: create, esc: esc, getJSON: getJSON };
})(window);
