// about-page.js - fills in the "Use your own AI assistant" section on the How
// It Works page: this deployment's MCP address and the tools it offers, read
// from /mcp-info so the page can't drift from the server. A separate file
// because the CSP forbids inline scripts.
(async () => {
  const urlEl = document.getElementById('mcp-url');
  if (!urlEl) return;
  const esc = (s) => String(s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let url = location.origin + '/mcp';
  try {
    const info = await (await fetch('/mcp-info')).json();
    if (info.url) url = info.url;
    const list = document.getElementById('tool-list');
    if (list && Array.isArray(info.tools)) {
      list.innerHTML = info.tools
        .map((t) => `<li><b>${esc(t.title)}.</b> ${esc(t.summary)}</li>`).join('');
    }
  } catch (e) { /* keep the address built from this page's own origin */ }
  urlEl.textContent = url;
  const btn = document.getElementById('copy-mcp');
  if (btn) {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(url);
        btn.textContent = 'Copied';
      } catch (e) {
        btn.textContent = 'Select and copy the address';
      }
      setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
    });
  }
})();
