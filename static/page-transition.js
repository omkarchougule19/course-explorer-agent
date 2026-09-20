/* page-transition.js
 *
 * Tells the browser which way a page change is going so the CSS in
 * theme-genz.css can slide the content left or right (cross-document View
 * Transitions: Chrome/Edge 126+, Safari 18.2+; other browsers ignore this).
 *
 * Direction: browser back/forward follows the history; moving between the
 * main tabs follows their order in the nav; anything else counts as forward.
 *
 * Must be a plain, synchronous script in <head>: pagereveal fires before the
 * first paint, so a deferred or async script would miss it.
 */
(function () {
  'use strict';

  var TAB_ORDER = ['/', '/departments.html', '/calendar.html', '/schedule.html',
                   '/freshness.html', '/about.html'];

  function tabIndex(url) {
    try {
      var p = new URL(url, location.href).pathname;
      if (p === '/index.html') p = '/';
      return TAB_ORDER.indexOf(p);
    } catch (e) { return -1; }
  }

  // 'backwards' | 'forwards' for a navigation from -> to (both entries).
  function direction(activation) {
    if (!activation || !activation.from || !activation.entry) return 'forwards';
    var from = activation.from, to = activation.entry;
    if (activation.navigationType === 'traverse') {
      return to.index < from.index ? 'backwards' : 'forwards';
    }
    var a = tabIndex(from.url), b = tabIndex(to.url);
    if (a >= 0 && b >= 0 && a !== b) return b < a ? 'backwards' : 'forwards';
    return 'forwards';
  }

  function tag(e) {
    if (!e.viewTransition || !e.viewTransition.types) return;
    // pageswap's activation is on the event; pagereveal's is on navigation.
    var act = e.activation || (window.navigation && window.navigation.activation);
    e.viewTransition.types.add(direction(act));
  }

  window.addEventListener('pageswap', tag);
  window.addEventListener('pagereveal', tag);
})();
