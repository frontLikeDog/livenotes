/* ==================================================================
   分享卡片：1080×1440 竖版图，四种模板（金句 / 要点 / 高光 / 观点）。

   观点卡（docs/41 G1'）由「观点提出者本人」转发：一个人名 + 一句主张 +
   一句原话 + 本场信息；二维码打开的是这一条观点的只读页，不是整场笔记。

   不联网生成图片：Canvas 在本机画，二维码只在局域网分享开启时出现。
   深色底 + 琥珀强调与界面同一套视觉，导出的 PNG 不依赖主题。
   ================================================================== */
(function () {
  'use strict';

  var W = 1080, H = 1440;
  var S = {
    data: null, tpl: 'quote', info: null, qr: null, qrText: '',
    claimIdx: 0, claimInfo: null, claimInfoId: '',
    dialog: null, canvas: null, ctx: null, rendered: 0
  };

  function font(size, weight) {
    return (weight || 400) + ' ' + size + 'px "Segoe UI Variable Text","PingFang SC",' +
      '"Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif';
  }
  function mono(size, weight) {
    return (weight || 400) + ' ' + size + 'px "Cascadia Mono","SFMono-Regular",Consolas,monospace';
  }

  function roundRect(ctx, x, y, w, h, r) {
    var rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  function wrap(ctx, text, maxWidth, maxLines) {
    var chars = String(text == null ? '' : text).replace(/\s+/g, ' ').split('');
    var lines = [], line = '';
    for (var i = 0; i < chars.length; i += 1) {
      var test = line + chars[i];
      if (ctx.measureText(test).width > maxWidth && line) {
        lines.push(line);
        line = chars[i];
        if (lines.length >= maxLines) return clip(lines, maxLines);
      } else {
        line = test;
      }
    }
    if (line) lines.push(line);
    return clip(lines, maxLines);
  }

  function clip(lines, maxLines) {
    if (lines.length <= maxLines) return lines;
    var out = lines.slice(0, maxLines);
    var last = out[maxLines - 1];
    out[maxLines - 1] = (last.length > 1 ? last.slice(0, -1) : last) + '…';
    return out;
  }

  function drawText(ctx, text, x, y, opts) {
    opts = opts || {};
    ctx.font = opts.font || font(32);
    ctx.fillStyle = opts.color || '#ECE9E2';
    ctx.textAlign = opts.align || 'left';
    ctx.textBaseline = opts.baseline || 'top';
    var lines = opts.width ? wrap(ctx, text, opts.width, opts.maxLines || 3)
      : [String(text == null ? '' : text)];
    var lh = opts.lineHeight || 48;
    lines.forEach(function (line, i) {
      ctx.fillText(line, x, y + i * lh);
    });
    return y + lines.length * lh;
  }

  function background(ctx) {
    ctx.fillStyle = '#0A0C0F';
    ctx.fillRect(0, 0, W, H);
    var glow = ctx.createRadialGradient(W * 0.18, H * 0.06, 20, W * 0.18, H * 0.06, 620);
    glow.addColorStop(0, 'rgba(231,182,105,.17)');
    glow.addColorStop(1, 'rgba(231,182,105,0)');
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, H);
    var glow2 = ctx.createRadialGradient(W * 0.9, H * 0.92, 30, W * 0.9, H * 0.92, 560);
    glow2.addColorStop(0, 'rgba(127,211,174,.08)');
    glow2.addColorStop(1, 'rgba(127,211,174,0)');
    ctx.fillStyle = glow2;
    ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = 'rgba(231,182,105,.14)';
    ctx.lineWidth = 2;
    ctx.strokeRect(36, 36, W - 72, H - 72);
  }

  function brand(ctx, y) {
    roundRect(ctx, 96, y, 58, 58, 14);
    ctx.fillStyle = '#E7B669';
    ctx.fill();
    ctx.font = font(30, 800);
    ctx.fillStyle = '#231704';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('L', 125, y + 31);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.font = font(30, 650);
    ctx.fillStyle = '#F7F5EF';
    ctx.fillText('LiveNotes', 176, y + 28);
    ctx.font = font(17, 500);
    ctx.fillStyle = '#6E7885';
    ctx.fillText('实时语记 · 本机笔记', 176, y + 52);
  }

  function footer(ctx, noteUrl, caption) {
    var y = H - 250;
    ctx.strokeStyle = 'rgba(43,51,61,.9)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(96, y);
    ctx.lineTo(W - 96, y);
    ctx.stroke();
    ctx.font = mono(17);
    ctx.fillStyle = '#6E7885';
    ctx.fillText('音频与逐字稿只保存在原设备', 96, y + 42);
    ctx.fillText('由 LiveNotes 在本机整理', 96, y + 70);
    if (S.qr && noteUrl && S.qrText === noteUrl) {
      var size = 140, qx = W - 96 - size, qy = y + 14;
      ctx.fillStyle = '#FFFFFF';
      roundRect(ctx, qx - 12, qy - 12, size + 24, size + 24, 12);
      ctx.fill();
      drawQr(ctx, S.qr, qx, qy, size);
      ctx.font = mono(15);
      ctx.fillStyle = '#9AA4B0';
      ctx.textAlign = 'right';
      ctx.fillText(caption || '扫码在手机上看这篇笔记', qx + size, qy + size + 34);
      ctx.textAlign = 'left';
    } else {
      ctx.font = mono(16);
      ctx.fillStyle = '#6E7885';
      ctx.textAlign = 'right';
      ctx.fillText('局域网分享未开启', W - 96, y + 42);
      ctx.textAlign = 'left';
    }
  }

  function drawQr(ctx, matrix, x, y, size) {
    var n = matrix.length;
    var cell = size / n;
    ctx.fillStyle = '#0A0C0F';
    for (var r = 0; r < n; r += 1) {
      for (var c = 0; c < n; c += 1) {
        if (matrix[r][c]) ctx.fillRect(x + c * cell, y + r * cell, Math.ceil(cell), Math.ceil(cell));
      }
    }
  }

  function titleBlock(ctx, title, y) {
    ctx.font = mono(19, 500);
    ctx.fillStyle = '#E7B669';
    ctx.fillText('NOTE · ' + (S.data.session.started_at || '').slice(0, 10), 96, y);
    y += 34;
    ctx.font = font(58, 700);
    ctx.fillStyle = '#F7F5EF';
    var end = drawText(ctx, title, 96, y, {
      font: font(58, 700), color: '#F7F5EF', width: W - 192, lineHeight: 76, maxLines: 3
    });
    return end + 26;
  }

  function quoteText() {
    var d = S.data;
    var hi = (d.highlights || [])[0];
    if (hi && (hi.quote || hi.text)) {
      return { text: hi.quote || hi.text, who: hi.speaker_label || '', t: hi.peak_t || hi.t_start || 0 };
    }
    var point = (d.points || []).filter(function (p) { return p.quote; })[0];
    if (point) {
      return { text: point.quote,
               who: point.claim_speaker || point.claim_speaker_label ||
                    (point.speakers || [])[0] || '', t: point.t_from || 0 };
    }
    return { text: d.summary.overview || '这场直播还没有留下可引用的原话。', who: '', t: 0 };
  }

  function drawQuote(ctx) {
    var y = titleBlock(ctx, S.data.session.title || '未命名直播', 210);
    var q = quoteText();
    y = Math.max(y + 26, 430);
    ctx.fillStyle = 'rgba(231,182,105,.9)';
    ctx.font = font(96, 700);
    ctx.fillText('“', 92, y - 12);
    y += 74;
    y = drawText(ctx, q.text, 132, y, {
      font: font(46, 550), color: '#ECE9E2', width: W - 264, lineHeight: 70, maxLines: 8
    });
    y += 26;
    ctx.font = mono(21);
    ctx.fillStyle = '#9AA4B0';
    var who = q.who || S.data.streamer || '主播';
    ctx.fillText('— ' + who + (q.t ? ' · ' + fmt(q.t) : ''), 132, y);
    if (S.data.summary.overview) {
      drawText(ctx, S.data.summary.overview, 96, H - 420, {
        font: font(26), color: '#9AA4B0', width: W - 192, lineHeight: 42, maxLines: 3
      });
    }
    footer(ctx, S.info && S.info.note_url);
  }

  function drawPoints(ctx) {
    var y = titleBlock(ctx, S.data.session.title || '未命名直播', 210);
    y += 2;
    if (S.data.summary.overview) {
      y = drawText(ctx, S.data.summary.overview, 96, y, {
        font: font(28), color: '#9AA4B0', width: W - 192, lineHeight: 46, maxLines: 2
      }) + 22;
    }
    var points = (S.data.points || []).slice(0, 4);
    if (!points.length) {
      drawText(ctx, '这场还没有整理出要点。可以先看逐字稿。', 96, y + 20, {
        font: font(30), color: '#9AA4B0', width: W - 192, lineHeight: 48, maxLines: 2
      });
    }
    points.forEach(function (p, i) {
      if (y > H - 360) return;
      ctx.font = mono(25, 600);
      ctx.fillStyle = '#E7B669';
      ctx.fillText(String(i + 1).padStart(2, '0'), 96, y + 2);
      var text = p.title || p.gist || '';
      var end = drawText(ctx, text, 158, y, {
        font: font(30, 550), color: '#ECE9E2', width: W - 254, lineHeight: 46, maxLines: 2
      });
      if (p.gist && p.gist !== text) {
        end = drawText(ctx, p.gist, 158, end + 2, {
          font: font(23), color: '#9AA4B0', width: W - 254, lineHeight: 36, maxLines: 2
        });
      }
      y = end + 30;
    });
    footer(ctx, S.info && S.info.note_url);
  }

  function drawHighlight(ctx) {
    var d = S.data;
    var hi = (d.highlights || [])[0] || {};
    var y = titleBlock(ctx, d.session.title || '未命名直播', 130);
    y += 12;
    ctx.font = mono(20);
    ctx.fillStyle = '#E7B669';
    ctx.fillText('高光 · ' + fmt(hi.peak_t || hi.t_start || 0)
      + (hi.speaker_label ? ' · ' + hi.speaker_label : ''), 96, y);
    y += 46;
    var quote = hi.quote || hi.text || hi.hook || d.summary.overview || '';
    y = drawText(ctx, quote, 96, y, {
      font: font(38, 550), color: '#ECE9E2', width: W - 192, lineHeight: 60, maxLines: 8
    });
    y += 22;
    if (hi.why) {
      drawText(ctx, hi.why, 96, y, {
        font: font(24), color: '#9AA4B0', width: W - 192, lineHeight: 38, maxLines: 2
      });
    }
    var bm = (d.bookmarks || [])[0];
    if (bm && bm.note) {
      drawText(ctx, '书签：' + bm.note, 96, H - 390, {
        font: font(24), color: '#9AA4B0', width: W - 192, lineHeight: 38, maxLines: 2
      });
    }
    footer(ctx, S.info && S.info.note_url);
  }

  function claimList() {
    var rows = (S.data && S.data.claims) || [];
    var out = rows.filter(function (c) {
      return c && c.stance && (c.kind === 'claim' || !c.kind);
    });
    return out.length ? out : rows.filter(function (c) { return c && c.stance; });
  }

  function currentClaim() {
    var list = claimList();
    if (!list.length) return null;
    return list[Math.min(S.claimIdx, list.length - 1)];
  }

  function drawChip(ctx, text, x, y) {
    ctx.font = font(24, 600);
    var w = ctx.measureText(text).width + 36;
    roundRect(ctx, x, y, w, 44, 22);
    ctx.fillStyle = 'rgba(231,182,105,.12)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(231,182,105,.3)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = '#E7B669';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x + 18, y + 23);
    ctx.textBaseline = 'top';
    return w;
  }

  function drawClaim(ctx) {
    var d = S.data;
    var list = claimList();
    var c = currentClaim();
    ctx.font = mono(19, 500);
    ctx.fillStyle = '#E7B669';
    ctx.fillText('VIEWPOINT · ' + (d.session.started_at || '').slice(0, 10), 96, 200);
    if (!c) {
      drawText(ctx, '这场还没有可分享的观点。', 96, 300, {
        font: font(40, 600), color: '#ECE9E2', width: W - 192, lineHeight: 60
      });
      drawText(ctx, '等笔记整理出「谁 · 什么观点」之后再来。', 96, 410, {
        font: font(26), color: '#9AA4B0', width: W - 192, lineHeight: 44
      });
      footer(ctx, '', '');
      return;
    }

    var y = 250;
    ctx.font = font(58, 750);
    ctx.fillStyle = '#F7F5EF';
    var who = c.who_label || c.who || '一位上麦者';
    y = drawText(ctx, who, 96, y, {
      font: font(58, 750), color: '#F7F5EF', width: W - 192,
      lineHeight: 74, maxLines: 2
    }) + 18;
    if (c.topic) {
      drawChip(ctx, c.topic, 96, y);
      y += 76;
    } else {
      y += 10;
    }

    ctx.font = font(92, 700);
    ctx.fillStyle = 'rgba(231,182,105,.9)';
    ctx.fillText('“', 92, y - 8);
    y += 78;
    y = drawText(ctx, c.stance, 132, y, {
      font: font(42, 550), color: '#ECE9E2', width: W - 264,
      lineHeight: 64, maxLines: 6
    });
    var quote = String(c.quote || '').trim();
    if (quote && quote !== c.stance) {
      y += 26;
      drawText(ctx, '「' + quote + '」', 132, y, {
        font: font(24), color: '#9AA4B0', width: W - 264,
        lineHeight: 40, maxLines: 3
      });
    }

    var n = (d.speakers || []).length || 1;
    var source = c.who_source_label || '';
    if (c.who_source === 'confirmed' || c.who_source === 'auto') source = '';
    var meta = '本场 ' + n + ' 人上麦 · 《' + (d.session.title || '未命名场次') + '》 · 由 LiveNotes 记录' +
      (source ? ' · ' + source : '');
    drawText(ctx, meta, 96, H - 330, {
      font: font(23), color: '#6E7885', width: W - 192,
      lineHeight: 38, maxLines: 2
    });
    footer(ctx, S.claimInfo && S.claimInfo.claim_url, '扫码查看这条观点');
  }

  function fmt(seconds) {
    var t = Math.max(0, Math.round(Number(seconds) || 0));
    var m = Math.floor(t / 60), s = t % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function render() {
    if (!S.ctx) return;
    var ctx = S.ctx;
    ctx.clearRect(0, 0, W, H);
    background(ctx);
    brand(ctx, 88);
    if (!S.data) return;
    if (S.tpl === 'points') drawPoints(ctx);
    else if (S.tpl === 'highlight') drawHighlight(ctx);
    else if (S.tpl === 'claim') drawClaim(ctx);
    else drawQuote(ctx);
    S.rendered += 1;
  }

  function noteInfo(claimId) {
    var url = '/api/share/info?session_id=' +
      encodeURIComponent(S.data.session.session_id);
    if (claimId) url += '&claim_id=' + encodeURIComponent(claimId);
    return fetch(url, { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  /* 观点卡的二维码指向「这一条观点」；换一条就要重新取一次链接。 */
  function ensureClaimInfo() {
    var c = currentClaim();
    var cid = c ? c.claim_id : '';
    if (!cid || (S.claimInfoId === cid && S.claimInfo)) {
      return Promise.resolve(S.claimInfo);
    }
    return noteInfo(cid).then(function (info) {
      if (info) {
        S.info = info;
        S.claimInfo = info;
        S.claimInfoId = cid;
        updateLink();
      }
      return info;
    });
  }

  /* 当前模板该带哪张二维码：观点卡用 claim_url，其他用 note_url。
     换模板/换观点都要重取，不能让上一张码跟着新链接走。 */
  function refreshQr() {
    var url = S.tpl === 'claim' ? (S.claimInfo && S.claimInfo.claim_url)
      : (S.info && S.info.note_url);
    if (!url || !S.info || !S.info.lan_enabled) {
      S.qr = null;
      render();
      return Promise.resolve();
    }
    return qrMatrix(url).then(function (qr) {
      S.qr = qr;
      render();
    });
  }

  function qrMatrix(text) {
    if (!text) return Promise.resolve(null);
    if (S.qr && S.qrText === text) return Promise.resolve(S.qr);
    return fetch('/api/qr?text=' + encodeURIComponent(text), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.rows) return null;
        S.qr = d.rows;
        S.qrText = text;
        return S.qr;
      })
      .catch(function () { return null; });
  }

  function updateLink() {
    var input = S.dialog.querySelector('#shareLink');
    if (!S.info) { input.value = '正在读取…'; return; }
    var isClaim = S.tpl === 'claim';
    var url = isClaim ? (S.claimInfo && S.claimInfo.claim_url) : S.info.note_url;
    input.value = url || '局域网分享未开启';
    var hint = S.dialog.querySelector('#shareHint');
    if (S.info.lan_enabled) {
      hint.textContent = isClaim
        ? '二维码打开的是这一条观点的只读页；手机和电脑需要连同一个网络。'
        : '二维码只在局域网内可访问；手机和电脑需要连同一个网络。';
    } else {
      hint.textContent = '在设置里开启「局域网分享」后，卡片上会出现二维码。';
    }
    updateClaimNav();
  }

  function updateClaimNav() {
    if (!S.dialog) return;
    var nav = S.dialog.querySelector('#shareClaimNav');
    if (!nav) return;
    var list = claimList();
    var on = S.tpl === 'claim' && list.length > 1;
    nav.hidden = !on;
    if (!on) return;
    var at = Math.min(S.claimIdx, list.length - 1);
    nav.querySelector('#shareClaimCount').textContent =
      (at + 1) + ' / ' + list.length + ' · ' +
      (list[at].who_label || list[at].who || '');
  }

  function build() {
    if (S.dialog) return S.dialog;
    var dialog = document.createElement('dialog');
    dialog.className = 'p1-dialog share-dialog';
    dialog.id = 'shareDialog';
    dialog.setAttribute('aria-labelledby', 'shareTitle');
    dialog.innerHTML =
      '<div class="dialog-head"><div><h2 id="shareTitle">分享这篇笔记</h2>' +
      '<p>只包含标题、要点、观点和高光，不包含音频与逐字稿。</p></div>' +
      '<button class="icon-btn" type="button" data-share-close aria-label="关闭">' +
      '<svg class="od-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" aria-hidden="true" ' +
      'style="--od-icon-size:16px"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>' +
      '<div class="share-body">' +
      '<div class="share-templates chip-row" role="group" aria-label="卡片模板">' +
      '<button class="chip is-on" type="button" data-share-tpl="quote">金句</button>' +
      '<button class="chip" type="button" data-share-tpl="points">要点</button>' +
      '<button class="chip" type="button" data-share-tpl="highlight">高光</button>' +
      '<button class="chip" type="button" data-share-tpl="claim">观点</button></div>' +
      '<div class="share-claim-nav" id="shareClaimNav" hidden>' +
      '<button class="icon-btn" type="button" id="shareClaimPrev" aria-label="上一条观点">‹</button>' +
      '<span id="shareClaimCount">1 / 1</span>' +
      '<button class="icon-btn" type="button" id="shareClaimNext" aria-label="下一条观点">›</button>' +
      '</div>' +
      '<div class="share-canvas-wrap"><canvas class="share-canvas" id="shareCanvas" ' +
      'width="' + W + '" height="' + H + '"></canvas></div>' +
      '<div id="shareNamePanel"></div>' +
      '<label class="share-link"><span>本地链接</span>' +
      '<input id="shareLink" type="text" readonly value="正在读取…"></label>' +
      '<p class="share-hint" id="shareHint">二维码只在局域网分享开启时出现。</p>' +
      '</div>' +
      '<div class="dialog-foot">' +
      '<button class="btn btn-ghost" type="button" id="shareCopy">复制链接</button>' +
      '<button class="btn btn-primary" type="button" id="shareDownload">下载图片</button>' +
      '</div>';
    document.body.appendChild(dialog);
    S.dialog = dialog;
    S.canvas = dialog.querySelector('#shareCanvas');
    S.ctx = S.canvas.getContext('2d');
    dialog.querySelector('[data-share-close]').addEventListener('click', function () {
      dialog.close();
    });
    dialog.addEventListener('click', function (e) {
      if (e.target === dialog) dialog.close();
    });
    dialog.addEventListener('close', function () {
      if (window.LNNames) window.LNNames.unmount(dialog.querySelector('#shareNamePanel'));
    });
    dialog.querySelector('.share-templates').addEventListener('click', function (e) {
      var button = e.target.closest('[data-share-tpl]');
      if (!button) return;
      S.tpl = button.getAttribute('data-share-tpl');
      Array.prototype.forEach.call(
        dialog.querySelectorAll('[data-share-tpl]'), function (chip) {
          chip.classList.toggle('is-on', chip === button);
        });
      updateLink();
      if (S.tpl === 'claim') {
        ensureClaimInfo().then(function () { return refreshQr(); });
      } else {
        render();
        refreshQr();
      }
    });
    dialog.querySelector('#shareClaimPrev').addEventListener('click', function () {
      var list = claimList();
      if (!list.length) return;
      S.claimIdx = (S.claimIdx - 1 + list.length) % list.length;
      S.claimInfo = null;
      ensureClaimInfo().then(function () { return refreshQr(); });
    });
    dialog.querySelector('#shareClaimNext').addEventListener('click', function () {
      var list = claimList();
      if (!list.length) return;
      S.claimIdx = (S.claimIdx + 1) % list.length;
      S.claimInfo = null;
      ensureClaimInfo().then(function () { return refreshQr(); });
    });
    dialog.querySelector('#shareDownload').addEventListener('click', function () {
      S.canvas.toBlob(function (blob) {
        if (!blob) { P1.toast('图片没有生成，再试一次。'); return; }
        var url = URL.createObjectURL(blob);
        var name = (S.data.session.title || '笔记').replace(/[\\/:*?"<>|]/g, ' ').slice(0, 40);
        P1.download(url, 'LiveNotes-' + name + '-' + S.tpl + '.png');
        P1.metric('share_card_export', { session_id: S.data.session.session_id, kind: S.tpl });
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      }, 'image/png');
    });
    dialog.querySelector('#shareCopy').addEventListener('click', function () {
      var text = S.tpl === 'claim'
        ? ((S.claimInfo && S.claimInfo.claim_url) || '')
        : ((S.info && S.info.note_url) || '');
      if (!text) { P1.toast('还没有可复制的局域网链接。'); return; }
      var done = function () { P1.toast('链接已复制'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function () {
          dialog.querySelector('#shareLink').select();
          P1.toast('按 Ctrl+C 复制链接');
        });
      } else {
        dialog.querySelector('#shareLink').select();
        P1.toast('按 Ctrl+C 复制链接');
      }
    });
    return dialog;
  }

  /* 分享前补名：只影响卡片上的「谁」，不阻塞下载、复制或二维码。 */
  function mountNames() {
    var box = S.dialog && S.dialog.querySelector('#shareNamePanel');
    if (!box || !window.LNNames || !S.data || !S.data.session) return;
    window.LNNames.mount(box, S.data.session.session_id, {
      limit: 5,
      onNamed: function (out) {
        // 命名后把当前页面里同一簇的展示名一起换掉，卡片不用整页重载。
        (S.data.claims || []).forEach(function (claim) {
          if (claim.speaker_id === out.speaker_id) {
            claim.who = out.name;
            claim.who_label = out.name;
            claim.who_source = 'confirmed';
            claim.who_source_label = '已确认';
          }
        });
        (S.data.points || []).forEach(function (point) {
          // 只改「归属人就是这一簇」的观点。证据里提到某人，不代表观点属于他；
          // 按证据命中会把 A 的观点改到被引用的 B 名下，还会把多人压成一人。
          if (point.claim_speaker_speaker_id !== out.speaker_id) return;
          point.claim_speaker = out.name;
          point.claim_speaker_label = out.name;
          point.claim_speaker_source = 'confirmed';
        });
        var oldLabel = '';
        (S.data.speakers || []).forEach(function (speaker) {
          if (speaker.speaker_id === out.speaker_id) {
            oldLabel = speaker.label || '';
            speaker.label = out.name;
            speaker.display_name = out.name;
          }
        });
        if (oldLabel) {
          (S.data.highlights || []).forEach(function (hi) {
            if (hi.speaker_label === oldLabel) hi.speaker_label = out.name;
          });
        }
        render();
        updateClaimNav();
      }
    });
  }

  function open(data) {
    S.data = data;
    S.claimIdx = 0;
    S.claimInfo = null;
    S.claimInfoId = '';
    S.qr = null;
    S.qrText = '';
    // 有观点就默认观点卡：这是 docs/41 G1' 的主传播单元；
    // 没有观点时保持原来的金句/要点逻辑。
    S.tpl = claimList().length ? 'claim'
      : ((data.highlights || []).length ? 'quote' : 'points');
    var dialog = build();
    Array.prototype.forEach.call(dialog.querySelectorAll('[data-share-tpl]'), function (chip) {
      chip.classList.toggle('is-on', chip.getAttribute('data-share-tpl') === S.tpl);
    });
    S.info = null;
    render();
    updateLink();
    if (!dialog.open) dialog.showModal();
    mountNames();
    var cid = S.tpl === 'claim' ? (currentClaim() || {}).claim_id : '';
    noteInfo(cid).then(function (info) {
      S.info = info;
      if (S.tpl === 'claim' && info) {
        S.claimInfo = info;
        S.claimInfoId = cid || '';
      }
      updateLink();
      render();
      refreshQr();
    });
  }

  window.ShareCard = { open: open };
})();
