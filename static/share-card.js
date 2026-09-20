// share-card.js — renders a 1080x1080 PNG "card" for a course and hands it to
// the native share sheet (phones) or downloads it (desktop). Loaded on pages
// that show the course quick-view; uses motion.js for the confirmation toast when it is loaded.
(function () {
  'use strict';

  function toast(msg) { if (window.Motion) window.Motion.toast(msg); }
  var rendering = false;   // ignore double-clicks while a card is being built

  function wrapLines(ctx, text, maxWidth, maxLines) {
    var words = String(text || '').split(/\s+/).filter(Boolean);
    var lines = [], line = '';
    for (var i = 0; i < words.length; i++) {
      var test = line ? line + ' ' + words[i] : words[i];
      if (ctx.measureText(test).width > maxWidth && line) {
        lines.push(line);
        line = words[i];
        if (lines.length === maxLines) break;
      } else {
        line = test;
      }
    }
    if (lines.length < maxLines && line) lines.push(line);
    // ellipsis if we cut text off
    var used = lines.join(' ').split(/\s+/).length;
    if (used < words.length && lines.length) {
      var last = lines[lines.length - 1].replace(/[.,;:\s]*$/, '');
      while (last.length > 1 && ctx.measureText(last + '…').width > maxWidth) last = last.slice(0, -1);
      lines[lines.length - 1] = last + '…';
    }
    return lines;
  }

  async function course(d) {
    if (rendering) return;
    rendering = true;
    try { await render(d); } finally { rendering = false; }
  }

  async function render(d) {
    var url = d.url || location.href;
    try { await document.fonts.load('800 150px "Bricolage Grotesque"'); await document.fonts.load('500 34px "Inter"'); } catch (e) { /* fall back to system fonts */ }
    var S = 1080;
    var c = document.createElement('canvas');
    c.width = S; c.height = S;
    var ctx = c.getContext('2d');

    var bg = ctx.createLinearGradient(0, 0, S, S);
    bg.addColorStop(0, '#0a0d16'); bg.addColorStop(1, '#241052');
    ctx.fillStyle = bg; ctx.fillRect(0, 0, S, S);
    var glow = ctx.createRadialGradient(S * 0.85, S * 0.1, 10, S * 0.85, S * 0.1, S * 0.7);
    glow.addColorStop(0, 'rgba(255,122,46,0.28)'); glow.addColorStop(1, 'rgba(255,122,46,0)');
    ctx.fillStyle = glow; ctx.fillRect(0, 0, S, S);
    var glow2 = ctx.createRadialGradient(S * 0.05, S * 0.95, 10, S * 0.05, S * 0.95, S * 0.6);
    glow2.addColorStop(0, 'rgba(124,92,255,0.24)'); glow2.addColorStop(1, 'rgba(124,92,255,0)');
    ctx.fillStyle = glow2; ctx.fillRect(0, 0, S, S);

    var pad = 90;
    ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = 'rgba(255,255,255,0.7)';
    ctx.font = '700 28px "Inter", system-ui, sans-serif';
    ctx.fillText('ILLINI COURSE COPILOT', pad, 130);

    var g = ctx.createLinearGradient(pad, 0, S - pad, 0);
    g.addColorStop(0, '#ffffff'); g.addColorStop(1, '#ffb27a');
    ctx.fillStyle = g;
    var codeSize = 170;   // shrink long codes ("CS 101H") until they fit the margins
    do {
      ctx.font = '800 ' + codeSize + 'px "Bricolage Grotesque", "Inter", system-ui, sans-serif';
      codeSize -= 6;
    } while (ctx.measureText(d.code || '').width > S - pad * 2 && codeSize > 60);
    ctx.fillText(d.code || '', pad, 330);

    var y = 430;
    if (d.title) {
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 60px "Bricolage Grotesque", "Inter", system-ui, sans-serif';
      wrapLines(ctx, d.title, S - pad * 2, 2).forEach(function (l) { ctx.fillText(l, pad, y); y += 74; });
      y += 14;
    }
    if (d.desc) {
      ctx.fillStyle = 'rgba(255,255,255,0.78)';
      ctx.font = '500 34px "Inter", system-ui, sans-serif';
      wrapLines(ctx, d.desc, S - pad * 2, 6).forEach(function (l) { ctx.fillText(l, pad, y); y += 50; });
    }

    // bottom "ask me" pill
    var host = '';
    try { host = new URL(url).host; } catch (e) { host = location.host; }
    var label = host + ' — ask about this course';
    ctx.font = '700 30px "Inter", system-ui, sans-serif';
    var w = ctx.measureText(label).width + 64;
    var pillGrad = ctx.createLinearGradient(pad, 0, pad + w, 0);
    pillGrad.addColorStop(0, '#c4431a'); pillGrad.addColorStop(1, '#c23a6b');
    ctx.fillStyle = pillGrad;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(pad, S - 210, w, 72, 36); else ctx.rect(pad, S - 210, w, 72);
    ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.fillText(label, pad + 32, S - 162);
    ctx.fillStyle = 'rgba(255,255,255,0.45)';
    ctx.font = '500 22px "Inter", system-ui, sans-serif';
    ctx.fillText('Independent student project. Not affiliated with the University of Illinois.', pad, S - 70);

    var blob = await new Promise(function (res) { c.toBlob(res, 'image/png'); });
    if (!blob) { toast('Couldn’t make that card. Copy the link instead.'); return; }
    var fname = String(d.code || 'course').replace(/\s+/g, '-') + '.png';
    var file = new File([blob], fname, { type: 'image/png' });

    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file], title: d.code, text: d.code + ' on Illini Course Copilot', url: url });
        return;
      } catch (e) {
        if (e && e.name === 'AbortError') return;   // user closed the share sheet
      }
    }
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).catch(function () {});
    toast('Card saved, link copied 📎');
  }

  window.ShareCard = { course: course };
})();
