// citations.js — real, clickable source links for data shown across the
// site, shared so every page cites the same way. URLs are the actual
// public UIUC endpoints this app's own scraper hits (see app/scraper.py,
// app/load_calendar.py) - not invented placeholders.
(function (global) {
  'use strict';

  // The public, human-facing UIUC course explorer (courses.illinois.edu),
  // as opposed to the XML API this app's scraper reads - see
  // app/scraper.py's BASE_URL/CATALOG_BASE_URL and its Referer header.
  // Every segment is DB-sourced, so each is percent-encoded - a stored value
  // can't add path segments or break out of the href it lands in.
  function courseExplorerUrl(year, semester, subject, courseNumber) {
    var url = 'https://courses.illinois.edu/schedule';
    var seg = function (v) { return '/' + encodeURIComponent(String(v)); };
    if (year && semester) {
      url += seg(year) + seg(semester);
      if (subject) {
        url += seg(subject);
        if (courseNumber) url += seg(courseNumber);
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
    return 'https://registrar.illinois.edu/' + encodeURIComponent(String(semester)) + '-'
      + encodeURIComponent(String(year)) + '-academic-calendar/';
  }

  var GRADES_URL = 'https://github.com/wadefagen/datasets';

  // UIUC's school id on RateMyProfessors (confirmed against RMP's own search
  // result URLs, not guessed). Linking to RMP's search - not scraping it -
  // carries none of the ToS/legal risk scraping their data would: see
  // DECISIONS.md's "RateMyProfessors: link out, don't scrape" entry.
  var RMP_SCHOOL_ID = '1112';

  // Stored instructor names are "Last, F" (surname, first initial - see
  // scraper.py's fetch_section_detail()). Verified live against RMP's search:
  // querying the full "Last, F" string degrades badly (RMP seems to OR-match
  // the tokens, e.g. "Beard, J" surfaced 760 mostly-unrelated results
  // because it also loosely matched on "J"), while the surname alone gives
  // clean results. So this strips to just the part before the comma.
  function rmpSearchUrl(instructor) {
    if (!instructor) return null;
    var surname = String(instructor).split(',')[0].trim();
    if (!surname || surname === '-') return null;
    return 'https://www.ratemyprofessors.com/search/professors/' + RMP_SCHOOL_ID
      + '?q=' + encodeURIComponent(surname);
  }

  function line(html) {
    return '<p class="citation">Source: ' + html + '</p>';
  }

  // Attribute-escapes the URL as a last line of defence; `text` is always a
  // fixed label from the calling page, never data.
  function link(url, text) {
    var href = String(url).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return '<a href="' + href + '" target="_blank" rel="noopener">' + text + '</a>';
  }

  global.Citations = {
    courseExplorerUrl: courseExplorerUrl,
    registrarCalendarUrl: registrarCalendarUrl,
    GRADES_URL: GRADES_URL,
    rmpSearchUrl: rmpSearchUrl,
    line: line,
    link: link,
  };
})(window);
