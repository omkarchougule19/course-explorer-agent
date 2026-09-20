// motion.js — the "alive" layer shared by every public page: cursor spotlight,
// button ripple, scroll reveals, table-row stagger, confetti, toast, shake, and
// a couple of easter eggs. Pure progressive enhancement: if this file fails to
// load the site works, it is just flatter. Exposes window.Motion for page
// scripts that want to trigger an effect (shake on bad input, confetti on a win).
(function () {
  'use strict';

  var reducedQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  var hoverQuery = window.matchMedia('(hover: hover)');
  var SHAKE_MS = 450;          // keep in sync with .shake in theme-genz.css
  var STAGGER_ROWS = 24;       // only the first screenful of a table animates in

  // ---- Toast ---------------------------------------------------------------
  // The live region exists (empty) from load, so screen readers register it
  // before its text changes; a node created with its text already set is
  // often not announced.
  var toastEl = null, toastTimer = 0;
  function ensureToast() {
    if (toastEl) return toastEl;
    toastEl = document.createElement('div');
    toastEl.className = 'fx-toast';
    toastEl.setAttribute('role', 'status');
    toastEl.setAttribute('aria-live', 'polite');
    document.body.appendChild(toastEl);
    return toastEl;
  }
  function toast(msg) {
    var el = ensureToast();
    el.textContent = msg;
    el.classList.remove('show');
    void el.offsetWidth;   // restart the transition for back-to-back toasts
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2600);
  }

  // ---- Shake (bad / empty input) -------------------------------------------
  function shake(el) {
    if (!el) return;
    el.classList.remove('shake');
    void el.offsetWidth;   // restart the animation if it is already running
    el.classList.add('shake');
    setTimeout(function () { el.classList.remove('shake'); }, SHAKE_MS + 150);
  }

  // ---- Confetti ------------------------------------------------------------
  // One shared canvas and one animation loop no matter how many bursts are
  // fired; the canvas is removed when the last piece fades.
  var COLORS = ['#ff7a2e', '#ff4d9a', '#7c5cff', '#4dc3ff', '#ffd23f', '#ffffff'];
  var canvas = null, ctx = null, parts = [], looping = false, lastFrame = 0;

  function ensureCanvas() {
    // reuse the live canvas, but rebuild it if the window was resized/rotated mid-burst
    if (canvas && canvas.dataset.w === String(window.innerWidth) + 'x' + window.innerHeight) return;
    if (canvas) canvas.remove();
    canvas = document.createElement('canvas');
    canvas.className = 'fx-confetti';
    canvas.setAttribute('aria-hidden', 'true');
    var dpr = Math.min(window.devicePixelRatio || 1, 2);   // 3x phones don't need 3x confetti
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    canvas.dataset.w = window.innerWidth + 'x' + window.innerHeight;
    document.body.appendChild(canvas);
    ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
  }

  function frame(now) {
    var dt = Math.min(32, now - lastFrame) / 16;
    lastFrame = now;
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    parts = parts.filter(function (p) {
      p.vy += 0.28 * dt;
      p.vx *= 0.99;
      p.x += p.vx * dt; p.y += p.vy * dt; p.r += p.vr * dt;
      p.life -= 0.011 * dt;
      if (p.life <= 0 || p.y > window.innerHeight + 20) return false;
      ctx.save();
      ctx.globalAlpha = Math.max(0, Math.min(1, p.life * 1.6));
      ctx.translate(p.x, p.y);
      ctx.rotate(p.r);
      ctx.fillStyle = p.c;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
      return true;
    });
    if (parts.length) {
      requestAnimationFrame(frame);
    } else {
      looping = false;
      canvas.remove();
      canvas = ctx = null;
    }
  }

  // `origin` is {x, y} in viewport px; defaults to upper-middle of the screen.
  function confetti(origin, count) {
    if (reducedQuery.matches) return;
    ensureCanvas();
    var ox = origin && origin.x != null ? origin.x : window.innerWidth / 2;
    var oy = origin && origin.y != null ? origin.y : window.innerHeight * 0.3;
    for (var i = 0, n = count || 70; i < n; i++) {
      var a = Math.random() * Math.PI * 2;
      var v = 4 + Math.random() * 8;
      parts.push({
        x: ox, y: oy,
        vx: Math.cos(a) * v, vy: Math.sin(a) * v - 5,
        w: 5 + Math.random() * 6, h: 3 + Math.random() * 5,
        r: Math.random() * 6.28, vr: (Math.random() - 0.5) * 0.4,
        c: COLORS[(Math.random() * COLORS.length) | 0],
        life: 1,
      });
    }
    if (!looping) {
      looping = true;
      lastFrame = performance.now();
      requestAnimationFrame(frame);
    }
  }

  function centerOf(el) {
    if (!el || !el.getBoundingClientRect) return null;
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  var celebrated = {};   // once per page load, not persisted
  function celebrateOnce(key, el) {
    if (celebrated[key]) return;
    celebrated[key] = true;
    confetti(centerOf(el), 60);
  }

  // ---- Cursor spotlight ----------------------------------------------------
  // Writes --mx/--my on the hovered surface once per frame; theme-genz.css's
  // ::before glow reads them.
  var rafId = 0, lastMove = null;
  document.addEventListener('pointermove', function (e) {
    if (!hoverQuery.matches || e.pointerType === 'touch') return;
    lastMove = e;
    if (rafId) return;
    rafId = requestAnimationFrame(function () {
      rafId = 0;
      var t = lastMove.target;
      var el = t && t.closest ? t.closest('.panel, .kpi, .banner') : null;
      // A table under the cursor gains nothing from a glow, and its rows are
      // the expensive part of the panel to restyle.
      if (!el || (t.closest && t.closest('table'))) return;
      var r = el.getBoundingClientRect();
      el.style.setProperty('--mx', (lastMove.clientX - r.left) + 'px');
      el.style.setProperty('--my', (lastMove.clientY - r.top) + 'px');
    });
  }, { passive: true });

  // ---- Button ripple -------------------------------------------------------
  document.addEventListener('pointerdown', function (e) {
    if (reducedQuery.matches) return;
    var btn = e.target.closest && e.target.closest('button');
    if (!btn || btn.disabled) return;
    var r = btn.getBoundingClientRect();
    var size = Math.max(r.width, r.height) * 2;
    var s = document.createElement('span');
    s.className = 'ripple';
    s.setAttribute('aria-hidden', 'true');
    s.style.width = s.style.height = size + 'px';
    s.style.left = (e.clientX - r.left - size / 2) + 'px';
    s.style.top = (e.clientY - r.top - size / 2) + 'px';
    btn.appendChild(s);
    s.addEventListener('animationend', function () { s.remove(); });
  });

  // ---- Scroll reveal -------------------------------------------------------
  function initReveal() {
    if (reducedQuery.matches || !('IntersectionObserver' in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        // Anything already scrolled past (top < 0) also counts as seen, so a
        // restored scroll position never leaves panels invisible.
        if (en.isIntersecting || en.boundingClientRect.top < 0) {
          en.target.classList.add('in');
          io.unobserve(en.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.04 });
    document.querySelectorAll('.panel, .section-label, footer, .cal-day').forEach(function (el) {
      if (el.getBoundingClientRect().top > window.innerHeight * 0.95) {
        el.classList.add('reveal');
        io.observe(el);
      }
    });
  }

  // ---- Table row stagger ---------------------------------------------------
  // CourseResults rebuilds the whole table via innerHTML on every render, so
  // watching direct children of .results-wrap (no subtree) is enough to catch
  // each new result set.
  function watchTables() {
    if (reducedQuery.matches) return;
    document.querySelectorAll('.results-wrap').forEach(function (wrap) {
      new MutationObserver(function () {
        var rows = wrap.querySelectorAll('tbody tr');
        for (var i = 0; i < rows.length && i < STAGGER_ROWS; i++) {
          rows[i].style.setProperty('--i', i);
          rows[i].classList.add('stagger');
        }
      }).observe(wrap, { childList: true });
    });
  }

  // ---- Easter eggs ---------------------------------------------------------
  // Konami code, or five quick clicks on the logo: confetti plus party mode
  // (the stat cards dance) for a few seconds.
  var KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
  var konamiPos = 0, partyTimer = 0;
  function party() {
    toast('🎉 You found the secret. Go Illini.');
    document.body.classList.add('party');   // stat cards dance; see theme-genz.css
    clearTimeout(partyTimer);
    partyTimer = setTimeout(function () { document.body.classList.remove('party'); }, 6000);
    [0, 350, 700, 1050].forEach(function (delay, i) {
      setTimeout(function () {
        confetti({ x: window.innerWidth * (0.2 + i * 0.2), y: window.innerHeight * 0.25 }, 70);
      }, delay);
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.repeat) return;
    var tag = e.target && e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { konamiPos = 0; return; }
    var k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    konamiPos = (k === KONAMI[konamiPos]) ? konamiPos + 1 : (k === KONAMI[0] ? 1 : 0);
    if (konamiPos === KONAMI.length) { konamiPos = 0; party(); }
  });
  var logoClicks = 0, logoTimer = 0;
  document.addEventListener('click', function (e) {
    if (!(e.target.closest && e.target.closest('.banner h1 .icon'))) return;
    logoClicks++;
    clearTimeout(logoTimer);
    logoTimer = setTimeout(function () { logoClicks = 0; }, 1200);
    if (logoClicks >= 5) { logoClicks = 0; party(); }
  });

  window.Motion = { toast: toast, shake: shake, confetti: confetti, celebrateOnce: celebrateOnce, centerOf: centerOf };

  function init() { ensureToast(); initReveal(); watchTables(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
