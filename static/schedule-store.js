// schedule-store.js — shared "My Schedule" cart, backed by localStorage.
// One array of section objects under one key. Used by index.html (adding
// sections while browsing), schedule.html (viewing/managing the cart), and
// every page's nav badge (just needs the count). Client-side only - there's
// no server-side "schedule" concept, matching the rest of this app's
// no-accounts design (see DECISIONS.md).
(function (global) {
  'use strict';

  var KEY = 'illini-schedule-cart';

  function getAll() {
    try {
      var raw = localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function saveAll(items) {
    try { localStorage.setItem(KEY, JSON.stringify(items)); } catch (e) { /* storage unavailable */ }
    updateBadges();
  }

  function has(crn) {
    return getAll().some(function (s) { return s.crn === crn; });
  }

  function add(section) {
    var items = getAll();
    if (items.some(function (s) { return s.crn === section.crn; })) return items;
    items.push(section);
    saveAll(items);
    return items;
  }

  function remove(crn) {
    var items = getAll().filter(function (s) { return s.crn !== crn; });
    saveAll(items);
    return items;
  }

  function clear() {
    saveAll([]);
  }

  function updateBadges() {
    var items = getAll();
    var els = document.querySelectorAll('.schedule-badge');
    for (var i = 0; i < els.length; i++) {
      els[i].textContent = items.length;
      els[i].hidden = items.length === 0;
    }
  }

  document.addEventListener('DOMContentLoaded', updateBadges);

  global.ScheduleStore = { getAll: getAll, saveAll: saveAll, has: has, add: add, remove: remove, clear: clear, updateBadges: updateBadges };
})(window);
