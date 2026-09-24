// theme.js — dark-mode toggle, shared across pages. Avoiding a flash of the
// wrong theme on load happens earlier, via theme-init.js, loaded
// synchronously in each page's <head> to set data-theme from localStorage
// before first paint; this file only wires up the toggle button's click
// behavior.
(function () {
  function current() {
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr) return attr;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('theme', theme); } catch (e) { /* storage unavailable */ }
  }
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      apply(current() === 'dark' ? 'light' : 'dark');
    });
  });
})();
