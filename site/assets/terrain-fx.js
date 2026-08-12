/* TerraForge 官网的地形场动效。
 *
 * 两个东西共用一套引擎：
 *   1. 全屏背景 —— 一片缓慢演化的等值线场，看上去像一块地形在呼吸
 *   2. 首屏项目名 —— TerraForge 字形当遮罩，里面填分层设色渐变 + 同一片等值线，
 *      再用一道测绘扫描线从左到右揭示
 *
 * 为什么是等高线而不是渐变球 / 粒子：本项目的四条管线里就有一条是「从 DEM 渲染
 * 等高线瓦片」，首屏那条签名色带也是真实的分层设色图例。背景用等值线，动效才是
 * 长在产品语义上的，而不是贴上去的装饰。
 *
 * 实现是标量场 + marching squares：值噪声（哈希，无依赖）在 (x, y, t) 上取样，
 * t 缓慢推进 —— 等值线于是像地形被慢慢抬升 / 削平那样蠕动，而不是整体平移。
 *
 * 零依赖、ES5、无构建。prefers-reduced-motion 下两个效果都不创建。
 */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var ctxOK = !!document.createElement('canvas').getContext;
  if (!ctxOK) return;

  /* ── 值噪声 ──────────────────────────────────────────────────────────
     32 位整数哈希 + 三线性插值 + smoothstep。用整数哈希而不是预生成置换表：
     省掉初始化，且任意坐标都能直接取样，不用管表长取模。 */
  function hash(x, y, z) {
    var n = (x * 374761393 + y * 668265263 + z * 1274126177) | 0;
    n = (n ^ (n >>> 13)) * 1274126177 | 0;
    return ((n ^ (n >>> 16)) >>> 0) / 4294967295;
  }

  function smooth(t) { return t * t * (3 - 2 * t); }

  function noise3(x, y, z) {
    var xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
    var xf = smooth(x - xi), yf = smooth(y - yi), zf = smooth(z - zi);

    var c000 = hash(xi,     yi,     zi),     c100 = hash(xi + 1, yi,     zi);
    var c010 = hash(xi,     yi + 1, zi),     c110 = hash(xi + 1, yi + 1, zi);
    var c001 = hash(xi,     yi,     zi + 1), c101 = hash(xi + 1, yi,     zi + 1);
    var c011 = hash(xi,     yi + 1, zi + 1), c111 = hash(xi + 1, yi + 1, zi + 1);

    var x00 = c000 + (c100 - c000) * xf, x10 = c010 + (c110 - c010) * xf;
    var x01 = c001 + (c101 - c001) * xf, x11 = c011 + (c111 - c011) * xf;
    var y0 = x00 + (x10 - x00) * yf, y1 = x01 + (x11 - x01) * yf;
    return y0 + (y1 - y0) * zf;
  }

  /* 两个八度就够了。再多只是让线变碎，在低对比度下看不出细节，白烧 CPU。 */
  function field(buf, w, h, fx, fy, t, warp) {
    var i = 0;
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        var v = noise3(x * fx, y * fy, t) * 0.65
              + noise3(x * fx * 2.3 + 11.7, y * fy * 2.3 + 5.1, t * 1.6) * 0.35;
        /* 纵向拉一点梯度，让等值线整体像山脊走向而不是均匀的斑点 */
        buf[i++] = v + (y / h - 0.5) * warp;
      }
    }
  }

  /* ── marching squares ────────────────────────────────────────────────
     只吐独立线段、不串成折线：本用途下线段与折线视觉无差别，而串接要维护
     邻接表和端点容差，代价远大于收益。段数量级在几百，一次 path 全描完。 */
  function march(buf, w, h, level, seg) {
    var n = 0;
    for (var y = 0; y < h - 1; y++) {
      var r0 = y * w, r1 = r0 + w;
      for (var x = 0; x < w - 1; x++) {
        var tl = buf[r0 + x], tr = buf[r0 + x + 1];
        var bl = buf[r1 + x], br = buf[r1 + x + 1];

        var c = (tl > level ? 8 : 0) | (tr > level ? 4 : 0) |
                (br > level ? 2 : 0) | (bl > level ? 1 : 0);
        if (c === 0 || c === 15) continue;

        /* 四条边上的插值交点。命名按边：T 上、R 右、B 下、L 左 */
        var Tx = x + (level - tl) / (tr - tl), Ty = y;
        var Rx = x + 1,                        Ry = y + (level - tr) / (br - tr);
        var Bx = x + (level - bl) / (br - bl), By = y + 1;
        var Lx = x,                            Ly = y + (level - tl) / (bl - tl);

        switch (c) {
          case 1:  case 14: seg[n++] = Lx; seg[n++] = Ly; seg[n++] = Bx; seg[n++] = By; break;
          case 2:  case 13: seg[n++] = Bx; seg[n++] = By; seg[n++] = Rx; seg[n++] = Ry; break;
          case 3:  case 12: seg[n++] = Lx; seg[n++] = Ly; seg[n++] = Rx; seg[n++] = Ry; break;
          case 4:  case 11: seg[n++] = Tx; seg[n++] = Ty; seg[n++] = Rx; seg[n++] = Ry; break;
          case 6:  case 9:  seg[n++] = Tx; seg[n++] = Ty; seg[n++] = Bx; seg[n++] = By; break;
          case 7:  case 8:  seg[n++] = Lx; seg[n++] = Ly; seg[n++] = Tx; seg[n++] = Ty; break;
          /* 5 与 10 是鞍点，两种连法都合法。这个尺度下肉眼无从分辨，固定取一种。 */
          case 5:  seg[n++] = Lx; seg[n++] = Ly; seg[n++] = Tx; seg[n++] = Ty;
                   seg[n++] = Bx; seg[n++] = By; seg[n++] = Rx; seg[n++] = Ry; break;
          case 10: seg[n++] = Tx; seg[n++] = Ty; seg[n++] = Rx; seg[n++] = Ry;
                   seg[n++] = Lx; seg[n++] = Ly; seg[n++] = Bx; seg[n++] = By; break;
        }
      }
    }
    return n;
  }

  function strokeSegs(g, seg, n, sx, sy, ox, oy) {
    g.beginPath();
    for (var i = 0; i < n; i += 4) {
      g.moveTo(seg[i]     * sx + ox, seg[i + 1] * sy + oy);
      g.lineTo(seg[i + 2] * sx + ox, seg[i + 3] * sy + oy);
    }
    g.stroke();
  }

  /* 等高线默认分层设色配色（与首屏那条图例、与应用里的 contourTintPalette 同源） */
  var TINTS = ['#5e8c61', '#8fbf6f', '#b6cf7e', '#dcd98e',
               '#d9b97e', '#c49a6c', '#ac7f58', '#8e6246'];

  var LEVELS = 11;   /* 等值线条数 */

  /* ══ 背景 ══════════════════════════════════════════════════════════ */
  function Background(canvas) {
    var g = canvas.getContext('2d');
    var cw = 0, ch = 0, gw = 0, gh = 0, buf = null, seg = null;
    var CELL = 26;   /* 网格边长（CSS 像素）。越小线越密，代价是 O(n²) 取样 */

    function resize() {
      cw = canvas.clientWidth; ch = canvas.clientHeight;
      /* 背景是极低对比度的纹理，1x 足够；上 DPR 只是让描边成本翻倍 */
      canvas.width = cw; canvas.height = ch;
      gw = Math.max(4, Math.ceil(cw / CELL) + 1);
      gh = Math.max(4, Math.ceil(ch / CELL) + 1);
      buf = new Float32Array(gw * gh);
      seg = new Float32Array(gw * gh * 8);
      g.lineCap = 'round';
    }

    function draw(t) {
      if (!buf) return;
      /* 0.115 的频率下，一条闭合等值线大约 180~260px —— 读起来是等高线，
         再低就变成模糊色块，再高就成了纹理噪点。0.055 的时间系数是「10 秒能
         看出变了、扫一眼看不出在动」这个量级，作为环境动效刚好。 */
      field(buf, gw, gh, 0.115, 0.115, t * 0.055, 0.35);
      g.clearRect(0, 0, cw, ch);
      var sx = cw / (gw - 1), sy = ch / (gh - 1);
      for (var k = 0; k < LEVELS; k++) {
        var lv = 0.28 + k * (0.44 / (LEVELS - 1));
        /* 每 5 条加粗一档 —— 计曲线，制图学里本来就是这么画的 */
        var index = (k % 5 === 0);
        g.lineWidth = index ? 1.15 : 0.75;
        /* alpha 上限约 26/255：暗底上刚好能认出线形，又不与正文抢对比度。
           实测 0.042/0.075 那一档最大 alpha 只有 19，几乎看不见。 */
        g.strokeStyle = index ? 'rgba(232,234,237,0.105)' : 'rgba(232,234,237,0.060)';
        strokeSegs(g, seg, march(buf, gw, gh, lv, seg), sx, sy, 0, 0);
      }
    }

    return { resize: resize, draw: draw };
  }

  /* ══ 首屏项目名 ═════════════════════════════════════════════════════ */
  function Wordmark(canvas, host) {
    var g = canvas.getContext('2d');
    var fb = host.querySelector('.brandmark-fallback');
    var dpr = 1, cw = 0, ch = 0, gw = 0, gh = 0, buf = null, seg = null;
    var textW = 0, baseline = 0, ready = false, fontStr = '', trackStr = 'normal';
    var TEXT = 'TerraForge';
    var CELL = 9;    /* 字里的等值线要比背景密得多，否则一个字母塞不进两条线 */

    function resize() {
      if (!fb) return false;
      cw = host.clientWidth;
      if (!cw) return false;
      dpr = Math.min(window.devicePixelRatio || 1, 2);

      /* 字号、字重、字距全部读回退文本的计算样式，不自己算。
         自己按「填满容器 96%」推算过一版，结果在窄屏与 CSS 的 clamp() 打架：
         390px 视口下画布算出 60px、CSS 是 41.6px，交接那一瞬字会跳一下。
         让 CSS 当字号唯一真源，两层就永远严丝合缝。 */
      var cs = window.getComputedStyle(fb);
      var fs = parseFloat(cs.fontSize);
      fontStr = cs.fontWeight + ' ' + fs + 'px ' + cs.fontFamily;
      trackStr = cs.letterSpacing;
      g.font = fontStr;
      /* letterSpacing 是 Canvas2D 的较新属性；不支持时退化为 0，字宽会略宽一点，
         但因为下面的 textW 是实测值，揭示范围仍然准确。 */
      if ('letterSpacing' in g) g.letterSpacing = cs.letterSpacing;

      var m = g.measureText(TEXT);
      textW = m.width;

      /* 高度取回退文本自己的盒高，画布于是与它逐像素重合 */
      ch = Math.round(fb.getBoundingClientRect().height);
      canvas.style.height = ch + 'px';
      canvas.width = Math.round(cw * dpr);
      canvas.height = Math.round(ch * dpr);

      /* 用实测的字形上下界把字垂直居中，而不是拍一个 0.8×ch 的经验值 —— 换字体
         或换字号后那个经验值就偏了。 */
      var asc = m.actualBoundingBoxAscent || fs * 0.72;
      var desc = m.actualBoundingBoxDescent || fs * 0.2;
      baseline = Math.round((ch - (asc + desc)) / 2 + asc);

      gw = Math.max(6, Math.ceil(cw / CELL) + 1);
      gh = Math.max(6, Math.ceil(ch / CELL) + 1);
      buf = new Float32Array(gw * gh);
      seg = new Float32Array(gw * gh * 8);
      ready = true;
      return true;
    }

    /* p 是揭示进度 0→1；到 1 之后字形常驻，里面的等值线继续缓慢蠕动 */
    function draw(t, p) {
      if (!ready) return;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, cw, ch);

      var revealX = textW * Math.min(p, 1);

      g.save();
      /* 扫描线尚未走完时，只画已经扫过的部分 */
      if (p < 1) { g.beginPath(); g.rect(0, 0, revealX, ch); g.clip(); }

      /* 1. 分层设色底：低海拔绿 → 高海拔褐，和图例同一套色 */
      var grad = g.createLinearGradient(0, ch, 0, 0);
      for (var i = 0; i < TINTS.length; i++) {
        grad.addColorStop(i / (TINTS.length - 1), TINTS[i]);
      }
      g.fillStyle = grad;
      g.fillRect(0, 0, cw, ch);

      /* 2. 等值线叠在色底上，字里于是有真实的地形纹理 */
      field(buf, gw, gh, 0.10, 0.16, t * 0.07, 0.55);
      var sx = cw / (gw - 1), sy = ch / (gh - 1);
      g.lineCap = 'round';
      for (var k = 0; k < LEVELS; k++) {
        var lv = 0.30 + k * (0.40 / (LEVELS - 1));
        var index = (k % 5 === 0);
        g.lineWidth = index ? 1.9 : 1.1;
        g.strokeStyle = index ? 'rgba(12,13,16,0.50)' : 'rgba(12,13,16,0.26)';
        strokeSegs(g, seg, march(buf, gw, gh, lv, seg), sx, sy, 0, 0);
      }

      /* 3. 只保留字形像素。destination-in 是这里唯一不需要离屏画布的做法。 */
      g.globalCompositeOperation = 'destination-in';
      /* 重设 font/letterSpacing：save/restore 会还原它们，而遮罩这一步必须与
         resize 时量 textW 用的是同一套度量，否则揭示范围与字宽对不上。 */
      g.font = fontStr;
      if ('letterSpacing' in g) g.letterSpacing = trackStr;
      g.textBaseline = 'alphabetic';
      g.fillStyle = '#000';
      g.fillText(TEXT, 0, baseline);
      g.globalCompositeOperation = 'source-over';
      g.restore();

      /* 4. 扫描线：领先于揭示边缘的一道亮线 + 辉光，测绘仪扫过的感觉 */
      if (p < 1) {
        var x = revealX;
        var glow = g.createLinearGradient(x - 42, 0, x + 6, 0);
        glow.addColorStop(0, 'rgba(56,189,248,0)');
        glow.addColorStop(1, 'rgba(56,189,248,0.30)');
        g.fillStyle = glow;
        g.fillRect(x - 42, 0, 48, ch);
        g.fillStyle = 'rgba(125,211,252,0.95)';
        g.fillRect(x - 1, 0, 2, ch);
      }
    }

    return {
      resize: resize,
      draw: draw,
      isReady: function () { return ready; }
    };
  }

  /* ══ 单一 rAF 驱动 ═════════════════════════════════════════════════
     两个画布共用一个循环并限到 ~24fps：场演化本来就极慢（0.055/s），
     再高的帧率肉眼无差别，只是白烧电。标签页隐藏时整个循环停掉。 */
  function start() {
    var bgEl = document.getElementById('tf-bg');
    var wmHost = document.querySelector('.brandmark');
    var wmEl = wmHost && wmHost.querySelector('.brandmark-fx');

    var bg = bgEl ? Background(bgEl) : null;
    var wm = wmEl ? Wordmark(wmEl, wmHost) : null;

    if (bg) bg.resize();
    if (wm) wm.resize();

    var t0 = 0, last = 0, revealStart = 0, handed = false;
    var FRAME = 1000 / 24;

    function loop(now) {
      requestAnimationFrame(loop);
      if (document.hidden) return;
      if (!t0) { t0 = now; revealStart = now + 280; }   /* 让首屏文字先浮起来一点 */
      if (now - last < FRAME) return;
      last = now;

      var t = (now - t0) / 1000;
      if (bg) bg.draw(t);
      if (wm && wm.isReady()) {
        var e = Math.max(0, (now - revealStart) / 1150);
        /* easeOutQuint，和 CSS 里那条 cubic-bezier(.22,1,.36,1) 同一个手感 */
        wm.draw(t, e >= 1 ? 1 : 1 - Math.pow(1 - e, 5));
        /* 只有真的画完一帧才隐藏回退文本。放在 resize 之后就交接，会留下一段
           「回退文本已隐藏、画布还空着」的窗口 —— 字体加载慢、或标签页在后台
           加载时那段窗口能长到几秒，首屏标题整块空白。 */
        if (!handed) { handed = true; wmHost.className += ' fx-on'; }
      }
    }

    var rt = null;
    window.addEventListener('resize', function () {
      clearTimeout(rt);
      rt = setTimeout(function () {
        if (bg) bg.resize();
        if (wm) wm.resize();
      }, 150);
    });

    requestAnimationFrame(loop);
  }

  /* 字名要量 Inter 的文本宽度，字体没到位量出来的是回退字体的宽度，
     字号会算错。等 fonts.ready；不支持 Font Loading API 就直接开始。 */
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(start);
  } else {
    start();
  }
})();
