/* ==================================================================
   精修交付工作台
   左侧项目列表，右侧报告编辑 / 质检 / 打包。
   依赖 p1.js 的取数、格式化与 toast，不引框架。
   ================================================================== */
(function () {
  'use strict';

  var P = window.P1;
  var S = { items: [], stats: {}, filter: 'all', cur: null, sessions: [],
    dirty: false, busy: false };

  var PACKAGE_PRICE = { standard: 999, deep: 1999, event: 4999 };
  var STATE_LABEL = { draft: '草稿', producing: '制作中', review: '待交付',
    delivered: '已交付', revising: '修订中' };

  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function esc(v) { return P.esc(v == null ? '' : v); }
  function money(cents) {
    var yuan = Math.round((Number(cents) || 0) / 100);
    return '¥' + yuan.toLocaleString('zh-CN');
  }
  function fmtDate(iso) {
    var s = String(iso || '');
    return s ? s.slice(0, 10) : '—';
  }
  function stateLabel(s) { return STATE_LABEL[s] || s || '—'; }
  function toast(msg) { P.toast(msg); }
  function fail(d) {
    var msg = (d && d.error) || '操作没有完成，稍后再试。';
    toast(msg);
    return msg;
  }
  function currentId() { return S.cur && S.cur.project ? S.cur.project.id : ''; }

  /* ---------------- 列表 ---------------- */

  function loadList() {
    P.api('/api/delivery/projects?state=' + encodeURIComponent(S.filter))
      .then(function (d) {
        if (d.error) { fail(d); return; }
        S.items = d.items || [];
        S.stats = d.stats || {};
        renderStats();
        renderList();
        var qid = new URLSearchParams(location.search).get('id');
        if (qid && (!S.cur || currentId() !== qid)) openProject(qid, true);
      }).catch(function () { toast('交付列表没有加载出来。'); });
  }

  function renderStats() {
    var st = S.stats || {};
    $('#dlvStatActive').textContent = String(st.active || 0);
    $('#dlvStatReview').textContent = String(st.review || 0);
    $('#dlvStatDelivered').textContent = String(st.delivered || 0);
    $('#dlvStatRevenue').textContent = st.revenue_cents ? money(st.revenue_cents) : '—';
  }

  function renderList() {
    var list = $('#dlvList');
    list.innerHTML = '';
    var empty = $('#dlvEmpty');
    empty.hidden = S.items.length > 0;
    if (!S.items.length) {
      var kicker = empty.querySelector('.empty-kicker');
      var title = empty.querySelector('h2');
      var desc = empty.querySelector('p');
      if (S.filter === 'all') {
        kicker.textContent = '还没有交付项目';
        title.textContent = '把一场录完的直播做成报告包。';
        desc.textContent = '选一场已经有逐字稿的直播，填客户和截止时间，草稿会自动生成。';
      } else {
        kicker.textContent = '这个筛选下没有项目';
        title.textContent = '换个筛选看看。';
        desc.textContent = '全部项目里可能还有制作中或已交付的记录。';
      }
      return;
    }
    S.items.forEach(function (p) {
      var card = document.createElement('button');
      card.type = 'button';
      card.className = 'dlv-card' + (S.cur && currentId() === p.id ? ' is-on' : '');
      card.setAttribute('role', 'listitem');
      card.setAttribute('data-dlv-id', p.id);
      card.innerHTML =
        '<div class="dlv-card-top"><strong>' + esc(p.event_name || p.session_title || '未命名交付') +
          '</strong><span class="dlv-badge is-' + esc(p.state) + '"><i></i>' +
          esc(stateLabel(p.state)) + '</span></div>' +
        '<div class="dlv-card-sub">' + esc(p.client_name || '未填客户') + ' · ' +
          esc(p.session_title || p.session_id) + '</div>' +
        '<div class="dlv-card-meta"><span>' + esc(fmtDate(p.due_at)) + '</span><span>' +
          esc(money(p.price_cents)) + '</span><span>v' + (p.report_version || 0) +
          '</span>' + (p.revision_count ? '<span>修订 ' + p.revision_count + '</span>' : '') +
        '</div>';
      card.addEventListener('click', function () { openProject(p.id); });
      list.appendChild(card);
    });
  }

  /* ---------------- 详情 ---------------- */

  function openProject(id, force) {
    if (!id) return;
    if (S.dirty && !force && !confirm('当前报告有未保存的修改，确定切换项目？')) return;
    P.api('/api/delivery/project?id=' + encodeURIComponent(id)).then(function (d) {
      if (d.error) { fail(d); return; }
      S.cur = d;
      S.dirty = false;
      renderDetail();
      var url = '/delivery?id=' + encodeURIComponent(id);
      try { history.replaceState(null, '', url); } catch (e) {}
      $$('#dlvList .dlv-card').forEach(function (c) {
        c.classList.toggle('is-on', c.getAttribute('data-dlv-id') === id);
      });
    }).catch(function () { toast('项目没有加载出来。'); });
  }

  function apply(d) {
    if (!d || d.error) { fail(d); return; }
    S.cur = d;
    S.dirty = false;
    renderDetail();
  }

  function renderDetail() {
    if (!S.cur || !S.cur.project) return;
    var p = S.cur.project;
    $('#dlvPlaceholder').hidden = true;
    $('#dlvDetail').hidden = false;

    $('#dlvKicker').textContent = 'DELIVERY · ' + (p.package_label || '标准复盘');
    $('#dlvTitle').textContent = p.event_name || p.session_title || '未命名交付';
    $('#dlvSub').textContent = (p.client_name || '未填客户') + ' · ' +
      (p.session_title || p.session_id) + ' · ' + P.durText(p.duration_s || 0);
    $('#dlvState').value = p.state || 'draft';
    $('#dlvDueChip').textContent = '截止 ' + fmtDate(p.due_at);
    $('#dlvEvent').value = p.event_name || '';
    $('#dlvClient').value = p.client_name || '';
    $('#dlvPackage').value = p.package_key || 'standard';
    $('#dlvDue').value = fmtDate(p.due_at) === '—' ? '' : fmtDate(p.due_at);
    $('#dlvPrice').value = String(Math.round((p.price_cents || 0) / 100));
    $('#dlvNotes').value = p.notes || '';

    renderChecklist(S.cur.checklist);
    renderQa(S.cur.qa);
    renderSections(S.cur.report);
    renderPackage(p.package, S.cur.built);
    renderDownload(p);
  }

  function renderChecklist(board) {
    var box = $('#dlvChecklist');
    box.innerHTML = '';
    if (!board) { $('#dlvChecklistStat').textContent = '—'; return; }
    $('#dlvChecklistStat').textContent = board.done + ' / ' + board.total + ' 项完成';
    (board.items || []).forEach(function (item) {
      var row = document.createElement('label');
      row.className = 'dlv-check' + (item.mode === 'auto'
        ? ' is-auto ' + (item.checked ? 'is-pass' : 'is-warn') : '');
      if (item.mode === 'manual') {
        var input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = !!item.checked;
        input.addEventListener('change', function () { toggleCheck(item.key, input.checked); });
        row.appendChild(input);
      } else {
        var dot = document.createElement('span');
        dot.className = 'dot';
        row.appendChild(dot);
      }
      var text = document.createElement('div');
      text.innerHTML = '<strong>' + esc(item.label) + '</strong>' +
        (item.detail ? '<span>' + esc(item.detail) + '</span>' : '');
      row.appendChild(text);
      box.appendChild(row);
    });
  }

  function toggleCheck(key, checked) {
    if (!S.cur) return;
    var manual = {};
    (S.cur.checklist.items || []).forEach(function (it) {
      if (it.mode === 'manual') manual[it.key] = !!it.checked;
    });
    manual[key] = !!checked;
    P.post('/api/delivery/update', { id: currentId(), checklist: manual })
      .then(apply).catch(function () { toast('勾选没有保存成功。'); });
  }

  function renderQa(qa) {
    var box = $('#dlvQa');
    box.innerHTML = '';
    if (!qa) { $('#dlvQaStat').textContent = '—'; return; }
    var counts = { pass: 0, warn: 0, fail: 0 };
    (qa.checks || []).forEach(function (c) { counts[c.status] = (counts[c.status] || 0) + 1; });
    $('#dlvQaStat').textContent = counts.fail ? counts.fail + ' 项阻断'
      : counts.warn ? counts.warn + ' 项注意' : '全部通过';
    (qa.checks || []).forEach(function (c) {
      var row = document.createElement('div');
      row.className = 'dlv-qa-row is-' + esc(c.status);
      var tag = c.status === 'pass' ? '通过' : c.status === 'warn' ? '注意' : '阻断';
      row.innerHTML = '<span class="tag">' + tag + '</span><div><strong>' +
        esc(c.label) + '</strong>' + (c.detail ? '<span> · ' + esc(c.detail) + '</span>' : '') +
        '</div>';
      box.appendChild(row);
    });
  }

  function renderSections(rep) {
    var box = $('#dlvSections');
    box.innerHTML = '';
    if (!rep || !(rep.sections || []).length) {
      box.innerHTML = '<div class="dlv-package">还没有报告。点上面的「重新生成草稿」。' +
        '</div>';
      return;
    }
    var kindName = { text: '文字', points: '要点', quotes: '金句', clips: '片段' };
    rep.sections.forEach(function (sec) {
      var details = document.createElement('details');
      details.className = 'dlv-section' + (sec.kind === 'clips' ? ' is-clips' : '');
      details.open = true;
      var summary = document.createElement('summary');
      var n = (sec.items || []).length;
      summary.innerHTML = '<strong>' + esc(sec.title) + '</strong>' +
        '<span class="kind">' + esc(kindName[sec.kind] || sec.kind) +
        (n ? ' · ' + n + ' 条' : '') + '</span>';
      details.appendChild(summary);
      var ta = document.createElement('textarea');
      ta.setAttribute('data-sec', sec.key);
      ta.setAttribute('aria-label', sec.title);
      ta.value = sec.body || '';
      ta.placeholder = sec.key === 'actions'
        ? '写 1–3 条下一场可以执行的动作……'
        : sec.key === 'questions'
          ? '写还需要客户确认的问题……'
          : '这一节还没有内容。';
      ta.addEventListener('input', function () { S.dirty = true; });
      details.appendChild(ta);
      if (sec.kind === 'clips' && n) {
        var hint = document.createElement('div');
        hint.className = 'empty-hint';
        hint.textContent = '打包时会按这里的时间码切 ' + n + ' 段 m4a 原声；改文字不会改时间码。';
        details.appendChild(hint);
      }
      box.appendChild(details);
    });
  }

  function renderPackage(info, built) {
    var box = $('#dlvPackage');
    box.innerHTML = '';
    info = info || {};
    if (!info.built_at) {
      $('#dlvPackageStat').textContent = '还没有打包';
      box.innerHTML = '<div>打包会生成：报告 Markdown / HTML、逐字稿 SRT / Markdown、' +
        '高光原声 m4a（有录音时）、README 与 zip。</div>';
      return;
    }
    $('#dlvPackageStat').textContent = '已打包 · ' + P.fmtStamp(info.built_at);
    var line = document.createElement('div');
    line.className = 'ok';
    line.textContent = '原声片段 ' + (info.clips || 0) + ' 段 · ' +
      (info.files || []).length + ' 个文件';
    box.appendChild(line);
    var files = document.createElement('div');
    files.className = 'files';
    (info.files || []).forEach(function (f) {
      var chip = document.createElement('span');
      chip.className = 'file';
      chip.textContent = f;
      files.appendChild(chip);
    });
    box.appendChild(files);
    (info.warnings || []).forEach(function (w) {
      var warn = document.createElement('div');
      warn.className = 'warn';
      warn.textContent = '注意：' + w;
      box.appendChild(warn);
    });
  }

  function renderDownload(p) {
    var a = $('#dlvDownload');
    var ready = p.package && p.package.built_at;
    a.hidden = !ready;
    a.href = ready ? '/api/delivery/download?id=' + encodeURIComponent(p.id) : '#';
  }

  /* ---------------- 动作 ---------------- */

  function collectSections() {
    var rep = S.cur && S.cur.report;
    if (!rep) return null;
    var values = {};
    $$('#dlvSections textarea[data-sec]').forEach(function (ta) {
      values[ta.getAttribute('data-sec')] = ta.value;
    });
    return (rep.sections || []).map(function (s) {
      return { key: s.key, title: s.title, kind: s.kind, items: s.items || [],
        body: values[s.key] != null ? values[s.key] : (s.body || '') };
    });
  }

  function saveReport(quiet) {
    if (!S.cur || !S.cur.report) { toast('先生成草稿。'); return Promise.resolve(); }
    var sections = collectSections();
    if (!sections) return Promise.resolve();
    S.busy = true;
    return P.post('/api/delivery/save', { id: currentId(), sections: sections })
      .then(function (d) {
        S.busy = false;
        if (d.error) { fail(d); return; }
        apply(d);
        if (!quiet) toast('报告已保存 · v' + (d.report ? d.report.version : ''));
      }).catch(function () { S.busy = false; toast('保存没有完成。'); });
  }

  function draft() {
    if (S.busy) return;
    if (S.dirty && !confirm('当前有未保存的修改，重新生成会丢掉它。继续？')) return;
    var force = false;
    if (S.cur && S.cur.report && S.cur.report.engine === 'manual') {
      force = confirm('这份报告已经手工编辑过。重新生成会新增一个草稿版本，继续？');
      if (!force) return;
    }
    S.busy = true;
    P.post('/api/delivery/draft', { id: currentId(), force: force })
      .then(function (d) {
        S.busy = false;
        if (d.error) { fail(d); return; }
        apply(d);
        toast('草稿已生成 · v' + (d.report ? d.report.version : ''));
      }).catch(function () { S.busy = false; toast('生成没有完成。'); });
  }

  function runQa() {
    P.post('/api/delivery/qa', { id: currentId() }).then(function (d) {
      if (d.error) { fail(d); return; }
      if (S.cur) {
        S.cur.qa = d.qa;
        S.cur.checklist = d.checklist || S.cur.checklist;
      }
      renderQa(d.qa);
      renderChecklist(S.cur && S.cur.checklist);
      toast(d.qa && d.qa.overall === 'pass' ? '质检通过'
        : d.qa && d.qa.overall === 'warn' ? '质检有几项注意' : '质检有阻断项');
    }).catch(function () { toast('质检没有完成。'); });
  }

  function buildPackage() {
    if (!S.cur || !S.cur.report) { toast('先生成报告草稿。'); return; }
    if (S.dirty) { toast('先保存报告，再打包。'); return; }
    S.busy = true;
    P.post('/api/delivery/build', { id: currentId() }).then(function (d) {
      S.busy = false;
      if (d.error) { fail(d); return; }
      apply(d);
      var info = d.built || {};
      toast('交付包已生成 · ' + (info.clips || 0) + ' 段原声');
    }).catch(function () { S.busy = false; toast('打包没有完成。'); });
  }

  function markDelivered() {
    if (!S.cur) return;
    if (!confirm('标记已交付？之后仍可修改并重新打包。')) return;
    P.post('/api/delivery/deliver', { id: currentId() })
      .then(function (d) { if (d.error) { fail(d); return; } apply(d); toast('已标记交付'); })
      .catch(function () { toast('标记没有完成。'); });
  }

  function addRevision() {
    var note = prompt('这次修订客户要改什么？（可留空）', '');
    if (note === null) return;
    P.post('/api/delivery/revision', { id: currentId(), note: note })
      .then(function (d) { if (d.error) { fail(d); return; } apply(d); toast('已记录一次修订'); })
      .catch(function () { toast('修订没有记上。'); });
  }

  function removeProject() {
    if (!S.cur) return;
    if (!confirm('删除这个交付项目？报告版本和本地交付包都会删除，直播笔记不受影响。')) return;
    P.post('/api/delivery/remove', { id: currentId() }).then(function (d) {
      if (d.error) { fail(d); return; }
      S.cur = null;
      S.dirty = false;
      $('#dlvDetail').hidden = true;
      $('#dlvPlaceholder').hidden = false;
      try { history.replaceState(null, '', '/delivery'); } catch (e) {}
      loadList();
      toast('已删除');
    }).catch(function () { toast('删除没有完成。'); });
  }

  function saveFields() {
    if (!S.cur) return;
    var price = Math.max(0, Math.round(Number($('#dlvPrice').value || 0) * 100));
    P.post('/api/delivery/update', {
      id: currentId(),
      event_name: $('#dlvEvent').value,
      client_name: $('#dlvClient').value,
      package: $('#dlvPackage').value,
      due_at: $('#dlvDue').value,
      price_cents: price
    }).then(function (d) {
      if (d.error) { fail(d); return; }
      apply(d);
      loadList();
      toast('项目信息已保存');
    }).catch(function () { toast('项目信息没有保存。'); });
  }

  function saveNotes() {
    if (!S.cur) return;
    P.post('/api/delivery/update', { id: currentId(), notes: $('#dlvNotes').value })
      .then(function (d) {
        if (d.error) { fail(d); return; }
        S.cur.project.notes = $('#dlvNotes').value;
        toast('备注已保存');
      }).catch(function () { toast('备注没有保存。'); });
  }

  /* ---------------- 新建 ---------------- */

  function defaultDue(days) {
    var d = new Date(Date.now() + (days || 3) * 86400000);
    return d.toISOString().slice(0, 10);
  }

  function openCreate() {
    var dialog = $('#dlvDialog');
    $('#dlvNewEvent').value = '';
    $('#dlvNewClient').value = '';
    $('#dlvNewPackage').value = 'standard';
    $('#dlvNewDue').value = defaultDue(3);
    $('#dlvNewPrice').value = '999';
    $('#dlvSession').innerHTML = '<option value="">正在读取…</option>';
    $('#dlvSessionHint').textContent = '正在读取最近的场次…';
    $('#dlvCreate').disabled = true;
    if (!dialog.open) dialog.showModal();
    P.api('/api/library/cards?limit=60&filter=all').then(function (d) {
      var items = (d && d.items) || [];
      S.sessions = items;
      var sel = $('#dlvSession');
      sel.innerHTML = '';
      if (!items.length) {
        sel.innerHTML = '<option value="">还没有可用场次</option>';
        $('#dlvSessionHint').textContent = '先在笔记库记录一场直播，再回来建交付。';
        return;
      }
      var usable = 0;
      items.forEach(function (it) {
        var empty = Number(it.n_utterances || 0) <= 0;
        var opt = document.createElement('option');
        opt.value = it.session_id;
        opt.disabled = empty;
        opt.textContent = (it.title || '未命名') + ' · ' + (it.started_at || '').slice(5, 16) +
          ' · ' + P.durText(it.duration_s || 0) + (empty ? ' · 无逐字稿' : '');
        sel.appendChild(opt);
        if (!empty && !usable) { sel.value = it.session_id; usable++; }
        else if (!empty) usable++;
      });
      if (!usable) {
        $('#dlvSessionHint').textContent = '最近 60 场里没有带逐字稿的场次，先完成一场转写。';
        return;
      }
      $('#dlvSessionHint').textContent = '共 ' + usable + ' 场可交付（最近 60 场）。';
      $('#dlvCreate').disabled = false;
    }).catch(function () {
      $('#dlvSessionHint').textContent = '场次没有加载出来，稍后再试。';
    });
  }

  function createProject() {
    var sid = $('#dlvSession').value;
    if (!sid) { toast('先选一场直播。'); return; }
    var button = $('#dlvCreate');
    button.disabled = true;
    button.textContent = '正在生成草稿…';
    P.post('/api/delivery/create', {
      session_id: sid,
      event_name: $('#dlvNewEvent').value,
      client_name: $('#dlvNewClient').value,
      package: $('#dlvNewPackage').value,
      due_at: $('#dlvNewDue').value,
      price_cents: Math.max(0, Math.round(Number($('#dlvNewPrice').value || 0) * 100))
    }).then(function (d) {
      button.disabled = false;
      button.textContent = '创建并生成草稿';
      if (d.error) { fail(d); return; }
      $('#dlvDialog').close();
      S.cur = d;
      S.dirty = false;
      renderDetail();
      loadList();
      toast('交付项目已创建');
    }).catch(function () {
      button.disabled = false;
      button.textContent = '创建并生成草稿';
      toast('创建没有完成。');
    });
  }

  /* ---------------- 事件 ---------------- */

  $('#dlvFilters').addEventListener('click', function (e) {
    var btn = e.target.closest('[data-dlv-filter]');
    if (!btn) return;
    S.filter = btn.getAttribute('data-dlv-filter');
    $$('#dlvFilters .chip').forEach(function (c) { c.classList.toggle('is-on', c === btn); });
    loadList();
  });

  $('#dlvNew').addEventListener('click', openCreate);
  $('#dlvEmptyNew').addEventListener('click', openCreate);
  $('#dlvCreate').addEventListener('click', createProject);
  $('#dlvNewPackage').addEventListener('change', function () {
    $('#dlvNewPrice').value = String(PACKAGE_PRICE[this.value] || 999);
    var days = this.value === 'deep' ? 2 : 3;
    $('#dlvNewDue').value = defaultDue(days);
  });
  $$('#dlvDialog [data-close-dialog]').forEach(function (b) {
    b.addEventListener('click', function () { $('#dlvDialog').close(); });
  });
  $('#dlvDialog').addEventListener('click', function (e) {
    if (e.target === $('#dlvDialog')) $('#dlvDialog').close();
  });

  $$('[data-dlv-act]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var act = btn.getAttribute('data-dlv-act');
      if (act === 'draft') draft();
      else if (act === 'save') saveReport(false);
      else if (act === 'qa') runQa();
      else if (act === 'build') buildPackage();
      else if (act === 'revision') addRevision();
      else if (act === 'remove') removeProject();
    });
  });

  $('#dlvState').addEventListener('change', function () {
    if (!S.cur) return;
    P.post('/api/delivery/update', { id: currentId(), state: this.value })
      .then(function (d) {
        if (d.error) { fail(d); loadList(); return; }
        S.cur = d;
        S.cur.project.state = d.project.state;
        loadList();
        toast('状态已更新为「' + stateLabel(d.project.state) + '」');
      }).catch(function () { toast('状态没有保存。'); });
  });

  ['dlvEvent', 'dlvClient', 'dlvPackage', 'dlvDue', 'dlvPrice'].forEach(function (id) {
    $('#' + id).addEventListener('change', saveFields);
  });
  $('#dlvSaveNotes').addEventListener('click', saveNotes);

  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === 's') {
      if (!$('#dlvDetail').hidden) { e.preventDefault(); saveReport(false); }
    }
  });
  window.addEventListener('beforeunload', function (e) {
    if (S.dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  loadList();
})();
