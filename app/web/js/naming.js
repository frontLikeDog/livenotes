/* ==================================================================
   按需补名：只列「这场谁值得记」的前 3–5 位。
   收尾页和分享前共用；改一次名字就注册一次声纹，下次自动认。
   跳过不会挡住出卡，所以这里永远不弹模态、不强制填写。
   ================================================================== */
(function () {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
  }

  function fmtDur(seconds) {
    var sec = Math.max(0, Math.round(Number(seconds) || 0));
    if (sec >= 3600) {
      return Math.floor(sec / 3600) + ' 小时 ' + Math.round(sec % 3600 / 60) + ' 分';
    }
    if (sec >= 60) return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
    return sec + ' 秒';
  }

  function toast(message) {
    if (window.P1 && window.P1.toast) { window.P1.toast(message); return; }
    var el = document.getElementById('lnNameToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'lnNameToast';
      el.className = 'ln-name-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.classList.remove('show'); }, 3600);
  }

  function sourceText(row) {
    if (!row.named) return '';
    if (row.source === 'confirmed') return '已确认';
    if (row.source === 'auto') return '声纹识别';
    if (row.source === 'nickname') return '直播间昵称';
    return row.source_label || '';
  }

  function rowHtml(row, rank) {
    var named = !!row.named;
    var source = sourceText(row);
    var preview = row.preview
      ? '<button class="ln-preview" type="button" data-preview="1">▶ 3 秒</button>'
      : '';
    var suggest = row.suggestion
      ? '<button class="ln-suggest" type="button" data-pid="' + esc(row.suggestion.person_id) +
        '" data-name="' + esc(row.suggestion.name) + '">像是「' + esc(row.suggestion.name) +
        '」</button>'
      : '';
    var edit = named ? ''
      : '<div class="ln-name-edit">' + suggest +
        '<input class="ln-name-input" type="text" maxlength="40" placeholder="输入名字，例如：阿哲">' +
        '<button class="ln-save" type="button">保存</button></div>';
    return '<div class="ln-name-row' + (named ? ' is-named' : '') +
      '" data-spk="' + esc(row.speaker_id) + '" data-named="' + (named ? '1' : '0') + '">' +
      '<div class="ln-name-main"><span class="ln-rank">' + esc(rank) + '</span>' +
      '<div class="ln-name-who"><b>' + esc(row.label) + '</b>' +
      '<small>' + esc(fmtDur(row.seconds)) + ' · ' + esc(row.n_utterances) + ' 句' +
      (source ? ' · ' + esc(source) : '') + '</small></div>' +
      '<div class="ln-name-side">' + preview + '</div></div>' +
      edit + '</div>';
  }

  function mount(box, sid, options) {
    if (!box) return;
    options = options || {};
    unmount(box);
    box.hidden = false;
    box.innerHTML = '<div class="ln-names-loading">正在看这一场谁说得最多…</div>';
    var token = {};
    box._lnToken = token;
    var url = '/api/sessions/top_speakers?session_id=' + encodeURIComponent(sid) +
      '&limit=' + encodeURIComponent(options.limit || 5);
    fetch(url, {cache: 'no-store'})
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (box._lnToken !== token) return;
        if (!data || !data.ok) {
          box.hidden = true;
          return;
        }
        var rows = data.speakers || [];
        if (!rows.length) { box.hidden = true; return; }
        var allNamed = rows.every(function (r) { return r.named; });
        var items = rows.map(function (row, index) {
          return rowHtml(row, index + 1);
        }).join('');
        box.innerHTML =
          '<div class="ln-names-head"><div class="ln-names-title">' +
          '<b>这场谁值得记</b><span>按发言时长 · 前 ' + rows.length + ' 位' +
          (data.top3 && data.top3.slots
            ? ' · 前 3 已命名 ' + data.top3.named + '/' + data.top3.slots : '') +
          '</span></div><button class="ln-names-skip" type="button">跳过</button></div>' +
          '<div class="ln-names-list">' + items + '</div>' +
          '<p class="ln-names-foot">' +
          (allNamed ? '这一场都说得出名字，直接出卡就行。'
            : (data.hint || '补一次名字，声纹入库，下次自动认。跳过不影响出卡。')) +
          '</p>';
        if (!data.has_audio) {
          box.querySelectorAll('.ln-preview').forEach(function (button) {
            button.disabled = true;
            button.title = '这一场没有原始音频，不能试听';
          });
        }
        wire(box, sid, data, options);
      })
      .catch(function () {
        if (box._lnToken === token) box.hidden = true;
      });
  }

  function wire(box, sid, data, options) {
    var audio = new Audio();
    audio.preload = 'none';
    box._lnAudio = audio;
    var playing = null;

    box.querySelector('.ln-names-skip').addEventListener('click', function () {
      pause();
      box.hidden = true;
    });

    function pause() {
      try { audio.pause(); } catch (e) { /* 没在播就忽略 */ }
      if (playing) {
        playing.textContent = '▶ 3 秒';
        playing = null;
      }
    }

    box.querySelectorAll('.ln-preview').forEach(function (button) {
      button.addEventListener('click', function () {
        var row = button.closest('.ln-name-row');
        var item = (data.speakers || []).filter(function (r) {
          return r.speaker_id === row.getAttribute('data-spk');
        })[0];
        if (!item || !item.preview || !data.has_audio) {
          toast('这一场没有可回放的原始音频');
          return;
        }
        if (playing === button) { pause(); return; }
        pause();
        audio.src = '/api/audio?session_id=' + encodeURIComponent(sid) +
          '&t_start=' + encodeURIComponent(item.preview.t_start) +
          '&t_end=' + encodeURIComponent(item.preview.t_end);
        audio.play().then(function () {
          playing = button;
          button.textContent = '■ 停止';
        }).catch(function (error) {
          // 连续点两段试听时，前一行的 play() 会被 pause()/换 src 打断，
          // 抛 AbortError 是预期行为，不能报「音频已被清理」。
          if (error && error.name === 'AbortError') return;
          toast('这段试听加载失败，可能音频已被清理');
        });
      });
    });
    audio.addEventListener('ended', pause);

    box.querySelectorAll('.ln-suggest').forEach(function (button) {
      button.addEventListener('click', function () {
        var edit = button.closest('.ln-name-edit');
        var input = edit && edit.querySelector('.ln-name-input');
        if (!input) return;
        input.value = button.getAttribute('data-name') || '';
        input.dataset.personId = button.getAttribute('data-pid') || '';
        input.focus();
      });
    });

    box.querySelectorAll('.ln-name-input').forEach(function (input) {
      input.addEventListener('input', function () {
        // 用户改过名字就不再按「就是这个已有的人」提交，避免把别人改名。
        var suggest = input.closest('.ln-name-edit').querySelector('.ln-suggest');
        if (suggest && input.value.trim() !== (suggest.getAttribute('data-name') || '')) {
          input.dataset.personId = '';
        }
      });
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          var button = input.closest('.ln-name-edit').querySelector('.ln-save');
          if (button) button.click();
        }
      });
    });

    box.querySelectorAll('.ln-save').forEach(function (button) {
      button.addEventListener('click', function () {
        var row = button.closest('.ln-name-row');
        var input = row.querySelector('.ln-name-input');
        var name = input ? input.value.trim() : '';
        if (!name) { toast('先输入一个名字'); input && input.focus(); return; }
        button.disabled = true;
        button.textContent = '保存中…';
        fetch('/api/speakers/name', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            session_id: sid,
            speaker_id: row.getAttribute('data-spk'),
            name: name,
            person_id: (input && input.dataset.personId) || ''
          })
        }).then(function (resp) { return resp.json(); }).then(function (out) {
          if (!out || !out.ok) throw new Error((out && out.error) || '保存失败');
          row.dataset.named = '1';
          row.className = 'ln-name-row is-named';
          row.querySelector('.ln-name-who b').textContent = out.name;
          var edit = row.querySelector('.ln-name-edit');
          if (edit) {
            var label = document.createElement('span');
            label.className = 'ln-name-source';
            label.textContent = out.enrolled ? '已入库 · 下次自动认'
              : (data.voiceprint === false ? '已命名 · 本场显示' : '已命名');
            edit.replaceWith(label);
          }
          var item = (data.speakers || []).filter(function (r) {
            return r.speaker_id === out.speaker_id;
          })[0];
          if (item) {
            item.label = out.name;
            item.named = true;
            item.source = 'confirmed';
            item.source_label = '已确认';
            item.person_id = out.person_id || item.person_id;
            item.suggestion = null;
          }
          toast(out.enrolled
            ? '已记下「' + out.name + '」，声纹入库，下次自动认'
            : (data.voiceprint === false
              ? '已记下「' + out.name + '」；云端导入的场次没有声纹，只改本场显示'
              : '已记下「' + out.name + '」；这一簇声纹太短，下次还不能自动认'));
          recalc(box, data);
          if (typeof options.onNamed === 'function') options.onNamed(out);
        }).catch(function (error) {
          toast('没有保存：' + error.message);
          button.disabled = false;
          button.textContent = '保存';
        });
      });
    });
  }

  function recalc(box, data) {
    var heading = box.querySelector('.ln-names-title span');
    if (!heading || !data.top3) return;
    var top3 = (data.speakers || []).slice(0, 3);
    var named = top3.filter(function (r) { return r.named; }).length;
    heading.textContent = '按发言时长 · 前 ' + (data.speakers || []).length + ' 位' +
      ' · 前 3 已命名 ' + named + '/' + top3.length;
    var all = (data.speakers || []).every(function (r) { return r.named; });
    var foot = box.querySelector('.ln-names-foot');
    if (foot && all) foot.textContent = '这一场都说得出名字，直接出卡就行。';
  }

  function unmount(box) {
    if (!box) return;
    box._lnToken = null;
    if (box._lnAudio) {
      try { box._lnAudio.pause(); } catch (e) { /* 已销毁 */ }
      box._lnAudio = null;
    }
    box.hidden = true;
    box.innerHTML = '';
  }

  window.LNNames = {mount: mount, unmount: unmount, fmtDur: fmtDur};
})();
