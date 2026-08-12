/* 滚动揭示（双向）。
 *
 * 原来内联在 index.html 底部。拆成外部文件是因为站点现在有两个页面（中文 /
 * 与英文 /en/），内联就得复制两份 —— 而英文页是生成的，复制进去的中文注释
 * 会被抓取端连同正文一起读走。
 *
 * 这里刻意不用 IntersectionObserver：它只在阈值被跨越时回调，而「一帧之内
 * 从视口下方跳到视口上方」并不产生跨越，回调不会触发，被跳过的区块就永久
 * 停在 opacity:0。点导航锚点跳转后往回滚、拖滚动条、刷新时浏览器恢复滚动
 * 位置这三种常见操作都会踩到，表现是整屏空白。
 *
 * 换成 rAF 节流的位置扫描：每一帧对每个元素重新判定当前该不该揭示，不依赖
 * 「跨越」这个事件，对跳跃、恢复、缩放窗口一律成立。
 *
 * 双向：元素滚出视口后撤销 .in，滚回来时动画重播。判定带滞回，否则元素停在
 * 阈值附近时一点点抖动就会反复加减 .in：
 *   揭示  top < vh * 0.92 且 bottom > 0
 *   撤销  bottom < -40   或 top > vh + 40
 *   两者之间保持现状 —— 这段就是滞回带。
 * 撤销只发生在元素完全离开视口再超出 40px 之后，那时它根本不在画面上，所以
 * 撤销本身永远不可见；用户看到的只有「滚回来时重新播一遍」。
 *
 * 因为要双向，元素揭示后不能出队，监听也不能摘。开销可以忽略：共 14 个容器，
 * rAF 节流下每帧 14 次 getBoundingClientRect()。
 */
(function () {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  // .legend 也要进队列：它的色带靠 .in 触发逐档展开，不是普通的 .rv 上浮。
  var els = [].slice.call(document.querySelectorAll('.rv, .legend'));
  var ticking = false;

  function sweep() {
    ticking = false;
    var vh = window.innerHeight;
    var edge = vh * 0.92;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var r = el.getBoundingClientRect();
      if (r.top < edge && r.bottom > 0) {
        el.classList.add('in');
      } else if (r.bottom < -40 || r.top > vh + 40) {
        el.classList.remove('in');
      }
    }
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(sweep);
  }

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  sweep();
})();
