// citations.js — real, clickable source links for data shown across the
// site, shared so every page cites the same way. URLs are the actual
// public UIUC endpoints this app's own scraper hits (see app/scraper.py,
// app/load_calendar.py) - not invented placeholders.
(function (global) {
  'use strict';

  // The public, human-facing UIUC course explorer (courses.illinois.edu),
  // as opposed to the XML API this app's scraper reads - see
  // app/scraper.py's BASE_URL/CATALOG_BASE_URL and its Referer header.
  function courseExplorerUrl(year, semester, subject, courseNumber) {
    var url = 'https://courses.illinois.edu/schedule';
    if (year && semester) {
      url += '/' + year + '/' + semester;
      if (subject) {
        url += '/' + subject;
        if (courseNumber) url += '/' + courseNumber;
      }
    }
    return url;
  }

  // The registrar publishes one page per term at a URL that isn't
  // uniformly derivable across all terms (see load_calendar.py's own
  // docstring) - archived terms use different paths. This pattern is
  // confirmed correct for the term(s) this app actually loads (current +
  // next), which are never archived, so it's safe for what this app shows,
  // just not guaranteed for an arbitrary past term.
  function registrarCalendarUrl(year, semester) {
    if (!year || !semester) return 'https://registrar.illinois.edu/';
    return 'https://registrar.illinois.edu/' + semester + '-' + year + '-academic-calendar/';
  }

  var GRADES_URL = 'https://github.com/wadefagen/datasets';

  function line(html) {
    return '<p class="citation">Source: ' + html + '</p>';
  }

  function link(url, text) {
    return '<a href="' + url + '" target="_blank" rel="noopener">' + text + '</a>';
  }

  global.Citations = {
    courseExplorerUrl: courseExplorerUrl,
    registrarCalendarUrl: registrarCalendarUrl,
    GRADES_URL: GRADES_URL,
    line: line,
    link: link,
  };
})(window);
