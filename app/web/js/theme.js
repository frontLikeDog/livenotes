/* ==================================================================
   主题引导（所有页面在 <head> 最先执行）

   目标：不管从哪条路径进来，页面都知道该用亮色还是深色。
     1. ?theme=studio|paper|dark|light  —— 宿主明确指定（桌面壳契约）；
     2. Cookie ln_theme                 —— 记住上一次解析结果，解决
        「壳里点进二级页 / 新标签页打开，URL 不带 theme」的情况；
     3. 系统 prefers-color-scheme       —— 纯网页版第一次打开时跟随系统；
     4. 兜底亮色。

   这里只落 data-theme 与 color-scheme，具体配色由各页 CSS 决定。
   桌面壳的 embed.js 之后可能把 data-theme 改成协议值 studio/paper，
   页面样式同时支持 studio/dark 与 paper/light 两组取值。
   ================================================================== */
(function () {
  var root = document.documentElement;
  var q = null;
  try { q = new URLSearchParams(location.search); } catch (e) {}

  function norm(v) {
    v = String(v || '').toLowerCase();
    if (v === 'paper' || v === 'light') return 'light';
    if (v === 'studio' || v === 'dark') return 'dark';
    return '';
  }

  var fromUrl = q ? norm(q.get('theme')) : '';
  var saved = '';
  try {
    var m = document.cookie.match(/(?:^|;\s*)ln_theme=(dark|light)/);
    if (m) saved = m[1];
  } catch (e) {}

  var explicit = !!(fromUrl || saved);
  var theme = fromUrl || saved;
  if (!theme && window.matchMedia) {
    try {
      theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch (e) {}
  }
  theme = theme || 'light';

  root.setAttribute('data-theme', theme);
  try { root.style.colorScheme = theme; } catch (e) {}

  // 宿主指定过主题就记住它，二级页不必再带参数。
  if (fromUrl) {
    try {
      document.cookie = 'ln_theme=' + theme + '; Path=/; SameSite=Lax; Max-Age=31536000';
    } catch (e) {}
  }

  // 没有明确选择时跟随系统切换；宿主自己会重载 iframe，不在这里抢方向。
  if (!explicit && window.matchMedia) {
    try {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onScheme = function (e) {
        var t = e.matches ? 'dark' : 'light';
        root.setAttribute('data-theme', t);
        try { root.style.colorScheme = t; } catch (err) {}
      };
      if (mq.addEventListener) mq.addEventListener('change', onScheme);
      else if (mq.addListener) mq.addListener(onScheme);
    } catch (e) {}
  }
})();
