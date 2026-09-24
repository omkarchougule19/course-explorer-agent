// theme-init.js - applies the saved light/dark theme before first paint.
// Loaded synchronously in each page's <head> (not deferred) so there's no
// flash of the wrong theme. Its own file, not inline, so the CSP can
// forbid inline scripts (script-src 'self').
var t;try{t=localStorage.getItem('theme');}catch(e){}document.documentElement.setAttribute('data-theme',t||'dark');
