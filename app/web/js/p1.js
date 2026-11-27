/* ==================================================================
   P1 页面公共层：取数、格式化、人话提示、全库搜索 / 问笔记面板。

   页面之间只共享这里；不引框架、不打包。嵌入宿主时 Ctrl+K 交给宿主，
   由壳统一打开自己的命令面板（页面仍可通过 LN.p1.openSearch 主动打开）。
   ================================================================== */
(function () {
  'use strict';

  var P1 = {};
  var embed = !!(window.LN && window.LN.host && window.LN.host.embed);

  /* ---------------- 基础工具 ---------------- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function api(url, opt) {
    opt = opt || {};
    opt.cache = 'no-store';
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        data.__status = r.status;
        return data;
      });
    });
  }
  function post(url, body) {
    return api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }
  function del(url) {
    return api(url, { method: 'DELETE' });
  }

  function debounce(fn, wait) {
    var timer = 0;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait || 200);
    };
  }

  function num(value) { var n = parseFloat(value); return isFinite(n) ? n : 0; }

  function fmtDur(seconds) {
    var t = Math.max(0, Math.round(num(seconds)));
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ':' : '') + pad(m) + ':' + pad(s);
  }
  function durText(seconds) {
    var t = Math.max(0, Math.round(num(seconds)));
    var h = Math.floor(t / 3600), m = Math.round((t % 3600) / 60);
    if (h && m) return h + ' 小时 ' + m + ' 分';
    if (h) return h + ' 小时';
    if (m) return m + ' 分钟';
    return t + ' 秒';
  }
  function fmtStamp(iso) {
    var s = String(iso || '');
    if (!s) return '—';
    return s.slice(5, 16).replace('T', ' ');
  }
  function fmtDay(iso) {
    var s = String(iso || '');
    if (s.length < 10) return '—';
    var d = new Date(s.slice(0, 10) + 'T00:00:00');
    if (isNaN(d.getTime())) return s.slice(5, 10);
    return s.slice(5, 10) + ' 周' + '日一二三四五六'.charAt(d.getDay());
  }
  function relTime(iso) {
    var s = String(iso || '');
    if (!s) return '刚刚';
    var d = new Date(s);
    if (isNaN(d.getTime())) return fmtStamp(s);
    var diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 90) return '刚刚';
    if (diff < 3600) return Math.round(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.round(diff / 3600) + ' 小时前';
    if (diff < 172800) return '昨天';
    return fmtStamp(s);
  }
  function tone(name) {
    var text = String(name || '?'), sum = 0;
    for (var i = 0; i < text.length; i += 1) sum += text.charCodeAt(i);
    return 'tone-' + (sum % 4 + 1);
  }
  function initial(name) {
    var text = String(name || '').trim();
    return text ? text.slice(0, 1) : '·';
  }
  function platformName(p) {
    return ({ douyin: '抖音', bilibili: 'B站', kuaishou: '快手' })[String(p || '')]
      || String(p || '');
  }
  function sourceName(mode) {
    return ({
      tab: '浏览器录音', direct: '直取音频', per_uid: '分轨录音',
      mixed: '混音录音', import: '云端导入', demo: '演示数据'
    })[String(mode || '')] || '本机记录';
  }
  var NOTE_STATES = {
    empty: ['还没有笔记', 'is-empty'],
    insufficient: ['内容太少', 'is-empty'],
    queued: ['排队中', 'is-running'],
    running: ['整理中', 'is-running'],
    ready: ['已生成', ''],
    partial: ['部分完成', ''],
    failed: ['没能生成', 'is-failed'],
    stale: ['内容有更新', '']
  };
  function noteState(state, progress) {
    var row = NOTE_STATES[String(state || '')] || ['未知状态', 'is-empty'];
    var label = row[0];
    if (state === 'running' && progress != null) label += ' ' + progress + '%';
    return { label: label, cls: row[1] };
  }

  function metric(event, data) {
    try {
      var body = { event: event };
      data = data || {};
      ['session_id', 'kind', 'count', 'ms', 'follow_id'].forEach(function (k) {
        if (data[k] != null) body[k] = data[k];
      });
      post('/api/metric/event', body).catch(function () {});
    } catch (e) {}
  }

  function nav(url) {
    if (!url) return;
    if (embed && window.LN && LN.post) { LN.post({ type: 'ln-navigate', href: url }); return; }
    location.href = url;
  }
  function noteUrl(sid, t) {
    var u = '/note?session_id=' + encodeURIComponent(sid || '');
    if (t != null && t !== '' && isFinite(parseFloat(t))) {
      u += '&t=' + encodeURIComponent(String(Math.max(0, parseFloat(t))));
    }
    return u;
  }
  function goNote(sid, t) {
    metric('note_open', { session_id: sid });
    nav(noteUrl(sid, t));
  }
  function playUrl(sid, t) {
    var u = '/?session=' + encodeURIComponent(sid || '');
    if (t != null && isFinite(parseFloat(t))) u += '&t=' + encodeURIComponent(t);
    return u;
  }

  function download(url, name) {
    var a = document.createElement('a');
    a.href = url;
    if (name) a.download = name;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { a.remove(); }, 0);
  }

  /* ---------------- toast ---------------- */
  var toastTimer = 0;
  function toast(message, opts) {
    opts = opts || {};
    var box = $('#p1Toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'p1Toast';
      box.className = 'toast';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    box.textContent = '';
    var span = document.createElement('span');
    span.textContent = String(message == null ? '' : message);
    box.appendChild(span);
    if (opts.action && opts.action.label) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'toast-action';
      button.textContent = opts.action.label;
      button.addEventListener('click', function () {
        box.hidden = true;
        if (opts.action.onClick) opts.action.onClick();
      });
      box.appendChild(button);
    }
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.hidden = true; }, opts.duration || 2800);
    return box;
  }

  /* ---------------- 搜索 / 问笔记 ---------------- */
  var palette = null;
  var S = {
    tab: 'notes', q: '', seq: 0, items: [], idx: 0,
    timer: 0, askTimer: 0, askSeq: 0, askId: 0, asking: false,
    answeredQ: '', askCtx: null
  };

  var TABS = [
    ['notes', '笔记'], ['lines', '逐字稿'], ['follows', '关注'], ['ask', '问笔记']
  ];

  function ensurePalette() {
    if (palette) return palette;
    palette = document.createElement('dialog');
    palette.className = 'search-palette';
    palette.id = 'p1Search';
    palette.setAttribute('aria-label', '搜索与提问');
    palette.innerHTML =
      '<div class="sp-search">' +
        '<svg class="od-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.7" stroke-linecap="round" aria-hidden="true" ' +
          'style="--od-icon-size:17px"><circle cx="11" cy="11" r="6.5"/>' +
          '<path d="m16 16 4 4"/></svg>' +
        '<input id="p1SpInput" type="text" autocomplete="off" spellcheck="false" ' +
          'placeholder="搜笔记、原话或主播，也可以直接问…" aria-label="搜索或提问">' +
        '<kbd>Esc</kbd>' +
      '</div>' +
      '<div class="sp-tabs" id="p1SpTabs" role="tablist" aria-label="搜索范围"></div>' +
      '<div class="sp-body" id="p1SpBody"></div>' +
      '<footer class="sp-foot"><span id="p1SpHint">输入关键词开始搜索；上方切换范围</span>' +
        '<span><kbd>↑</kbd><kbd>↓</kbd> 选择 <kbd>Enter</kbd> 打开</span></footer>';
    document.body.appendChild(palette);
    $('#p1SpTabs', palette).innerHTML = TABS.map(function (t) {
      return '<button type="button" role="tab" data-sp-tab="' + t[0] + '" ' +
        'aria-selected="false">' + t[1] + '</button>';
    }).join('');
    var input = $('#p1SpInput', palette);
    input.addEventListener('input', function () {
      S.q = input.value;
      // 旧结果立即作废：输入后马上按 Enter 时不能点到上一次的建议。
      S.items = [];
      S.idx = 0;
      S.answeredQ = '';
      // 手动改字 = 新问题，不再挂在上一轮追问线程上。
      S.askCtx = null;
      clearTimeout(S.timer);
      S.timer = setTimeout(renderPalette, 220);
    });
    input.addEventListener('keydown', onPaletteKey);
    $('#p1SpTabs', palette).addEventListener('click', function (e) {
      var button = e.target.closest('[data-sp-tab]');
      if (!button) return;
      S.tab = button.getAttribute('data-sp-tab');
      clearTimeout(S.askTimer);
      renderPalette();
      input.focus();
    });
    palette.addEventListener('click', function (e) {
      if (e.target === palette) closeSearch();
    });
    palette.addEventListener('cancel', function (e) {
      e.preventDefault();
      closeSearch();
    });
    return palette;
  }

  function openSearch(tab, seed) {
    ensurePalette();
    S.tab = tab || 'notes';
    S.q = seed || '';
    S.idx = 0;
    S.items = [];
    S.answeredQ = '';
    S.askCtx = null;
    clearTimeout(S.askTimer);
    var input = $('#p1SpInput', palette);
    input.value = S.q;
    if (!palette.open) palette.showModal();
    renderPalette();
    requestAnimationFrame(function () {
      input.focus();
      if (S.q) input.select();
    });
  }
  function closeSearch() {
    if (!palette) return;
    clearTimeout(S.askTimer);
    if (palette.open) palette.close();
  }

  function setHint(text) {
    var hint = $('#p1SpHint');
    if (hint) hint.textContent = text;
  }

  function renderPalette() {
    if (!palette || !palette.open) return;
    $$('#p1SpTabs [data-sp-tab]', palette).forEach(function (button) {
      var on = button.getAttribute('data-sp-tab') === S.tab;
      button.classList.toggle('is-on', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var body = $('#p1SpBody', palette);
    body.innerHTML = '';
    S.items = [];
    S.idx = 0;
    var seq = ++S.seq;
    if (S.tab === 'notes') { renderNotes(body, seq); return; }
    if (S.tab === 'lines') { renderLines(body, seq); return; }
    if (S.tab === 'follows') { renderFollows(body, seq); return; }
    renderAsk(body, seq);
  }

  function group(text) {
    var p = document.createElement('p');
    p.className = 'sp-group';
    p.textContent = text;
    return p;
  }

  function emptyBox(title, text) {
    var box = document.createElement('div');
    box.className = 'sp-empty';
    var strong = document.createElement('strong');
    strong.textContent = title;
    box.appendChild(strong);
    box.appendChild(document.createTextNode(text));
    return box;
  }

  function highlight(text, query) {
    var safe = esc(text);
    var q = String(query || '').trim();
    if (!q) return safe;
    try {
      var re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      return safe.replace(re, '<em class="sp-highlight">$1</em>');
    } catch (e) { return safe; }
  }

  function addItem(body, opts) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'sp-item';
    var main = document.createElement('span');
    main.className = 'sp-item-main';
    var title = document.createElement('span');
    title.className = 'sp-item-title';
    title.innerHTML = highlight(opts.title, S.q);
    var sub = document.createElement('span');
    sub.className = 'sp-item-sub';
    sub.innerHTML = highlight(opts.sub || '', S.q);
    main.appendChild(title);
    if (opts.sub) main.appendChild(sub);
    var meta = document.createElement('span');
    meta.className = 'sp-item-meta';
    meta.textContent = opts.meta || '';
    button.appendChild(main);
    button.appendChild(meta);
    button.addEventListener('click', function () {
      if (opts.onClick) opts.onClick();
    });
    body.appendChild(button);
    S.items.push(button);
    return button;
  }

  function busy(body, text) {
    var box = document.createElement('div');
    box.className = 'sp-empty';
    box.innerHTML = '<span class="sp-spinner"></span> ' + esc(text || '正在找…');
    body.appendChild(box);
  }

  function renderNotes(body, seq) {
    var q = S.q.trim();
    setHint(q ? '找到与「' + q + '」相关的笔记内容' : '输入关键词开始搜索；上方切换范围');
    if (!q) {
      busy(body, '正在读最近的笔记…');
      api('/api/library/cards?limit=8&filter=all').then(function (d) {
        if (seq !== S.seq) return;
        body.innerHTML = '';
        body.appendChild(group('最近笔记'));
        var items = d.items || [];
        if (!items.length) {
          body.appendChild(emptyBox('笔记库还是空的', '先记录一场直播，结束后会自动整理。'));
          return;
        }
        items.forEach(function (item) {
          var meta = noteState(item.note_state, item.progress);
          addItem(body, {
            title: item.title || '未命名直播',
            sub: item.summary || meta.label,
            meta: fmtStamp(item.started_at),
            onClick: function () { closeSearch(); goNote(item.session_id, null); }
          });
        });
        selectFirst();
      });
      return;
    }
    busy(body, '正在搜索笔记…');
    api('/api/search?q=' + encodeURIComponent(q) + '&types=notes&limit=40')
      .then(function (d) {
        if (seq !== S.seq) return;
        body.innerHTML = '';
        body.appendChild(group('笔记'));
        var rows = d.rows || [];
        if (!rows.length) {
          body.appendChild(emptyBox('没有匹配的笔记', '换一个更短的词，或切到「问笔记」直接问。'));
          addAskSuggest(body, q);
          return;
        }
        rows.forEach(function (row) {
          var kind = ({ summary: '概述', point: '要点', highlight: '高光',
            bookmark: '书签' })[row.kind] || '要点';
          addItem(body, {
            title: row.session_title || row.title || '未命名直播',
            sub: row.text || '',
            meta: kind + (row.t_start != null ? ' · ' + fmtDur(row.t_start) : ''),
            onClick: function () { closeSearch(); goNote(row.session_id, row.t_start); }
          });
        });
        selectFirst();
      });
  }

  function addAskSuggest(body, seed) {
    var box = document.createElement('div');
    box.className = 'sp-suggests';
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'sp-suggest';
    button.textContent = '问笔记：「' + String(seed || '').slice(0, 18) + '」';
    button.addEventListener('click', function () {
      S.tab = 'ask';
      renderPalette();
    });
    box.appendChild(button);
    body.appendChild(box);
  }

  function renderLines(body, seq) {
    var q = S.q.trim();
    setHint(q ? '找到与「' + q + '」相关的原话' : '输入关键词，搜索所有场次的逐字稿');
    if (!q) {
      body.appendChild(emptyBox('搜一句原话', '记得半句就行。比如一个人名、一个数字，或一句结论。'));
      api('/api/library/cards?limit=6&filter=all').then(function (d) {
        if (seq !== S.seq) return;
        var words = [];
        (d.items || []).forEach(function (item) {
          (item.topics || []).forEach(function (t) {
            if (words.indexOf(t) < 0) words.push(t);
          });
        });
        if (!words.length) return;
        body.appendChild(group('试试这些主题'));
        var box = document.createElement('div');
        box.className = 'sp-suggests';
        words.slice(0, 8).forEach(function (word) {
          var chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'sp-suggest';
          chip.textContent = word;
          chip.addEventListener('click', function () {
            S.q = word;
            $('#p1SpInput').value = word;
            renderPalette();
          });
          box.appendChild(chip);
        });
        body.appendChild(box);
      });
      return;
    }
    busy(body, '正在翻逐字稿…');
    api('/api/search?q=' + encodeURIComponent(q) + '&types=utterances&limit=40')
      .then(function (d) {
        if (seq !== S.seq) return;
        body.innerHTML = '';
        body.appendChild(group('逐字稿原话'));
        var rows = d.rows || [];
        if (!rows.length) {
          body.appendChild(emptyBox('没有匹配的原话', '试试更短的关键词，或换一个说法。'));
          return;
        }
        rows.forEach(function (row) {
          addItem(body, {
            title: row.text || '',
            sub: (row.session_title || row.title || '') + (row.speaker_label ? ' · ' + row.speaker_label : ''),
            meta: row.t_start != null ? fmtDur(row.t_start) : '',
            onClick: function () { closeSearch(); goNote(row.session_id, row.t_start); }
          });
        });
        selectFirst();
      });
  }

  function renderFollows(body, seq) {
    setHint('关注保存在本机，开播检测也在本机完成');
    busy(body, '正在读取关注…');
    api('/api/follows').then(function (d) {
      if (seq !== S.seq) return;
      body.innerHTML = '';
      body.appendChild(group('关注的主播'));
      var items = d.items || [];
      if (!items.length) {
        body.appendChild(emptyBox('还没有关注', '到「关注」页粘贴直播间链接，开播时就能提醒你。'));
        return;
      }
      items.forEach(function (item) {
        var state = item.recording ? '正在记录' : item.live_state === 'live' ? '直播中'
          : item.live_state === 'unknown' ? '检测失败' : '未开播';
        addItem(body, {
          title: item.name || item.room_key || '未命名主播',
          sub: state + (item.live_title ? ' · ' + item.live_title : ''),
          meta: platformName(item.platform),
          onClick: function () { closeSearch(); nav('/follows'); }
        });
      });
      selectFirst();
    });
  }

  function renderAsk(body, seq) {
    var q = S.q.trim();
    setHint(q ? '答案只从你的笔记里找，并标出出处' : '问一句，答案只从你的笔记里找，并标出出处');
    if (!q) {
      busy(body, '正在准备…');
      api('/api/qa/suggest').then(function (d) {
        if (seq !== S.seq) return;
        body.innerHTML = '';
        if (d.enabled === false) { loadAskHistory(body, seq); return; }
        body.appendChild(group('可以这样问'));
        var box = document.createElement('div');
        box.className = 'sp-suggests';
        (d.items || []).forEach(function (question) {
          var chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'sp-suggest';
          chip.textContent = question;
          chip.addEventListener('click', function () {
            S.q = question;
            $('#p1SpInput').value = question;
            renderPalette();
          });
          box.appendChild(chip);
        });
        if (!(d.items || []).length) {
          box.appendChild(document.createTextNode('还没有足够的笔记。先记录一场，再回来问。'));
        }
        body.appendChild(box);
        loadAskHistory(body, seq);
      });
      return;
    }
    runAsk(q, seq);
  }

  function loadAskHistory(body, seq) {
    api('/api/qa/history?limit=5').then(function (d) {
      if (seq !== S.seq) return;
      var items = (d.items || []).filter(function (item) {
        return item.state === 'ready' || item.state === 'no_result';
      });
      if (!items.length) return;
      body.appendChild(group('最近问过'));
      items.slice(0, 4).forEach(function (item) {
        addItem(body, {
          title: item.question,
          sub: item.answer || (item.state === 'no_result' ? '没有找到直接相关的内容' : ''),
          meta: relTime(item.created_at),
          onClick: function () {
            S.askCtx = null;
            S.q = item.question;
            $('#p1SpInput').value = item.question;
            renderPalette();
          }
        });
      });
    });
  }

  function runAsk(question, seq) {
    var body = $('#p1SpBody');
    clearTimeout(S.askTimer);
    S.asking = true;
    body.innerHTML = '';
    body.appendChild(group('问笔记'));
    var box = document.createElement('div');
    box.className = 'sp-answer';
    box.innerHTML = '<div class="sp-answer-kicker"><span class="sp-spinner"></span>' +
      ' 正在翻你的笔记…</div><p>先找相关内容，再组织答案。</p>';
    body.appendChild(box);
    S.items = [];
    // 请求体单独取名：上面的 body 是 DOM 节点，重名会被后面的赋值悄悄换掉。
    var payload = { question: question };
    if (S.askCtx) {
      payload.thread_id = S.askCtx.thread_id || '';
      payload.parent_id = S.askCtx.parent_id || 0;
    }
    post('/api/qa/ask', payload).then(function (d) {
      if (seq !== S.seq) return;
      if (!d.ok) {
        box.innerHTML = '<div class="sp-answer-kicker">没问成</div><p>' +
          esc(d.error || '再试一次。') + '</p>';
        S.asking = false;
        return;
      }
      if (d.state === 'ready' || d.state === 'no_result' || d.state === 'failed') {
        showAnswer(box, d, question);
        return;
      }
      S.askId = d.id;
      pollAnswer(box, d.id, question, seq);
    }).catch(function () {
      box.innerHTML = '<div class="sp-answer-kicker">没问成</div><p>网络或本地服务没有响应，再试一次。</p>';
      S.asking = false;
    });
  }

  function pollAnswer(box, id, question, seq) {
    var tries = 0;
    var tick = function () {
      if (seq !== S.seq) return;
      tries += 1;
      if (tries > 120) {
        box.innerHTML = '<div class="sp-answer-kicker">等太久了</div>' +
          '<p>可以稍后在「问笔记」里再来一次，录音和笔记都还在。</p>';
        S.asking = false;
        return;
      }
      api('/api/qa/answer?id=' + encodeURIComponent(id)).then(function (d) {
        if (seq !== S.seq) return;
        if (d.state === 'ready' || d.state === 'no_result' || d.state === 'failed') {
          showAnswer(box, d, question);
          return;
        }
        S.askTimer = setTimeout(tick, 800);
      }).catch(function () {
        S.askTimer = setTimeout(tick, 1200);
      });
    };
    S.askTimer = setTimeout(tick, 600);
  }

  function showAnswer(box, data, question) {
    S.asking = false;
    S.answeredQ = question;
    box.innerHTML = '';
    var kicker = document.createElement('div');
    kicker.className = 'sp-answer-kicker';
    if (data.state === 'failed') {
      kicker.textContent = '没答成';
      box.appendChild(kicker);
      var fail = document.createElement('p');
      fail.textContent = data.error || '模型没有生成可用回答。请稍后重试。';
      box.appendChild(fail);
      return;
    }
    if (data.state === 'no_result') {
      kicker.textContent = '没有找到直接相关的内容';
      box.appendChild(kicker);
      var p = document.createElement('p');
      p.textContent = '这几篇笔记里没有讲到「' + question + '」。换一个更具体的词，或先记录一场相关直播。';
      box.appendChild(p);
      return;
    }
    var cites = data.citations || [];
    kicker.textContent = '问笔记 · ' + cites.length + ' 条出处';
    box.appendChild(kicker);
    var answer = document.createElement('p');
    answer.textContent = data.answer || '';
    box.appendChild(answer);
    if (cites.length) {
      var wrap = document.createElement('div');
      wrap.className = 'sp-cites';
      cites.forEach(function (cite) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'sp-cite';
        var text = document.createElement('span');
        text.textContent = cite.text || '';
        var meta = document.createElement('b');
        meta.textContent = (cite.title || '笔记').slice(0, 10) +
          (cite.t_text ? ' · ' + cite.t_text : '');
        button.appendChild(text);
        button.appendChild(meta);
        button.addEventListener('click', function () {
          metric('qa_cite_click', { session_id: cite.session_id });
          closeSearch();
          goNote(cite.session_id, cite.t);
        });
        wrap.appendChild(button);
      });
      box.appendChild(wrap);
    }
    renderFollowups(box, data);
  }

  /* 追问：点一下接着问，服务端会带上一轮答案（只带摘要 + 引用编号）。
     追问不是新话题，所以 S.askCtx 只在点击时设置；手动改字会清掉。 */
  function renderFollowups(box, data) {
    var items = data.followups || [];
    if (!items.length) return;
    metric('qa_followup_shown', { count: items.length,
      session_id: (data.scope || {}).session_id || '' });
    var wrap = document.createElement('div');
    wrap.className = 'sp-followups';
    items.forEach(function (item) {
      var q = typeof item === 'string' ? item : (item.q || '');
      var why = typeof item === 'string' ? '' : (item.why || '');
      if (!q) return;
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'sp-followup';
      button.textContent = q;
      if (why) button.title = why;
      button.addEventListener('click', function () {
        metric('qa_followup_click', {
          session_id: (data.scope || {}).session_id || '',
          kind: 'followup'
        });
        S.askCtx = {
          thread_id: data.thread_id || String(data.id || ''),
          parent_id: Number(data.id) || 0
        };
        S.q = q;
        $('#p1SpInput').value = q;
        renderPalette();
      });
      wrap.appendChild(button);
    });
    if (wrap.childNodes.length) box.appendChild(wrap);
  }

  function selectFirst() {
    if (!S.items.length) return;
    S.idx = 0;
    S.items[0].classList.add('is-on');
  }
  function move(delta) {
    if (!S.items.length) return;
    S.items[S.idx].classList.remove('is-on');
    S.idx = (S.idx + delta + S.items.length) % S.items.length;
    S.items[S.idx].classList.add('is-on');
    S.items[S.idx].scrollIntoView({ block: 'nearest' });
  }
  function onPaletteKey(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return; }
    if (e.key === 'Enter') {
      if (S.asking) return;
      e.preventDefault();
      if (S.items.length && !S.q.trim() && S.tab !== 'ask') { S.items[S.idx].click(); return; }
      if (S.tab === 'ask') {
        // 空输入时 Enter 选建议/历史；有输入时只提交这次输入的问题。
        if (!S.q.trim() && S.items.length) { S.items[S.idx].click(); return; }
        if (!S.q.trim() || S.q === S.answeredQ) return;
        renderPalette();
        return;
      }
      if (S.items.length) S.items[S.idx].click();
    }
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    $$('[data-p1-search]').forEach(function (button) {
      button.addEventListener('click', function () {
        openSearch(button.getAttribute('data-p1-tab') || 'notes');
      });
    });
    // 桌面壳可以从命令面板直接打开「问笔记」：/library?search=ask&q=...
    var params = new URLSearchParams(location.search);
    var autoTab = params.get('search') || '';
    if (TABS.some(function (t) { return t[0] === autoTab; })) {
      requestAnimationFrame(function () {
        openSearch(autoTab, params.get('q') || '');
      });
    }
    if (!embed) {
      document.addEventListener('keydown', function (e) {
        var mod = e.ctrlKey || e.metaKey;
        if (mod && String(e.key || '').toLowerCase() === 'k') {
          e.preventDefault();
          openSearch('notes');
        }
      });
    }
    if (window.LN) {
      window.LN.p1 = {
        openSearch: openSearch, closeSearch: closeSearch,
        noteUrl: noteUrl, goNote: goNote, toast: toast
      };
    }
  }

  P1.$ = $; P1.$$ = $$; P1.esc = esc; P1.api = api; P1.post = post; P1.del = del;
  P1.debounce = debounce; P1.pad = pad;
  P1.fmtDur = fmtDur; P1.durText = durText; P1.fmtStamp = fmtStamp;
  P1.fmtDay = fmtDay; P1.relTime = relTime;
  P1.tone = tone; P1.initial = initial;
  P1.platformName = platformName; P1.sourceName = sourceName;
  P1.noteState = noteState; P1.metric = metric;
  P1.nav = nav; P1.noteUrl = noteUrl; P1.goNote = goNote; P1.playUrl = playUrl;
  P1.download = download; P1.toast = toast;
  P1.openSearch = openSearch; P1.closeSearch = closeSearch;
  P1.embed = embed;
  P1.perf = null;
  P1.perfReady = api('/api/perf').then(function (d) {
    P1.perf = d;
    var e = (d && d.effective) || {};
    document.documentElement.setAttribute(
      'data-perf', e.animations === false ? 'lite' : 'full');
    return d;
  }).catch(function () { return null; });
  window.P1 = P1;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
