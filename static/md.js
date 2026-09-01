// md.js — tiny Markdown -> HTML renderer for the assistant's answers.
//
// Why this exists instead of a library: the page's Content-Security-Policy
// only allows same-origin scripts (see app/api.py), so no CDN, and the agent
// only ever emits a small, predictable subset of Markdown — GitHub-style
// pipe tables, **bold**, `code`, bullet / numbered lists, and paragraphs.
// This handles exactly that in ~90 lines with no dependency.
//
// Security: the input is untrusted LLM output. Every piece of text is passed
// through escapeHtml() BEFORE any formatting markup is added, so no raw HTML
// from the model can reach innerHTML. Inline formatting only ever inserts a
// fixed set of tags (<strong>, <code>, <th>, <td>, ...).

(function (global) {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  // Applied to already-escaped text. `code` first so ** inside a code span
  // stays literal; then **bold**, then *italic* (the italic pattern requires
  // a non-space, non-word boundary on both sides so "3 * 4" isn't matched).
  function inline(escaped) {
    return escaped
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])/g, '<em>$1</em>');
  }

  function isTableSeparator(line) {
    var t = line.trim();
    if (t.indexOf('-') === -1) return false;
    return /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/.test(t);
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) {
      return c.trim();
    });
  }

  function renderTable(header, rows) {
    var html = '<table class="chat-table"><thead><tr>';
    header.forEach(function (c) { html += '<th>' + inline(escapeHtml(c)) + '</th>'; });
    html += '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>';
      for (var i = 0; i < header.length; i++) {
        html += '<td>' + inline(escapeHtml(r[i] == null ? '' : r[i])) + '</td>';
      }
      html += '</tr>';
    });
    return html + '</tbody></table>';
  }

  function render(src) {
    var lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var para = [];
    var i = 0;

    function flushPara() {
      if (!para.length) return;
      out.push('<p>' + para.map(function (l) { return inline(escapeHtml(l)); }).join('<br>') + '</p>');
      para = [];
    }

    while (i < lines.length) {
      var line = lines[i];

      if (!line.trim()) { flushPara(); i++; continue; }

      // pipe table: this row has a pipe and the next row is a --- separator
      if (line.indexOf('|') !== -1 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushPara();
        var header = splitRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') !== -1) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        out.push(renderTable(header, rows));
        continue;
      }

      // ATX heading
      var h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        flushPara();
        var level = Math.min(6, h[1].length + 2);
        out.push('<h' + level + '>' + inline(escapeHtml(h[2].trim())) + '</h' + level + '>');
        i++;
        continue;
      }

      // unordered list
      if (/^\s*[-*]\s+/.test(line)) {
        flushPara();
        var ul = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          ul.push('<li>' + inline(escapeHtml(lines[i].replace(/^\s*[-*]\s+/, ''))) + '</li>');
          i++;
        }
        out.push('<ul>' + ul.join('') + '</ul>');
        continue;
      }

      // ordered list
      if (/^\s*\d+[.)]\s+/.test(line)) {
        flushPara();
        var ol = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          ol.push('<li>' + inline(escapeHtml(lines[i].replace(/^\s*\d+[.)]\s+/, ''))) + '</li>');
          i++;
        }
        out.push('<ol>' + ol.join('') + '</ol>');
        continue;
      }

      para.push(line);
      i++;
    }
    flushPara();
    return out.join('');
  }

  render.escape = escapeHtml;
  global.renderMarkdown = render;
})(window);
