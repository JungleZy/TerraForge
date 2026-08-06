/**
 * 底部状态栏的随包底图解压进度。
 *
 * 事件 base_unpack_progress 的载荷是 {phase, fraction, error}，
 * phase ∈ idle | running | ready | failed（见 src/services/base_terrain_warmup.py）。
 * 服务端在 connect 时也推一次快照 —— 解压失败是终态，中途/事后连上的客户端
 * 只能靠那一次快照拿到。
 *
 * ⚠️ **本文件同时是 /config 与 /history 两页唯一的 socket 连接触发者。**
 * socket.js 刻意不在解析期建连（见它的文件头），连接由第一个 get() 的消费者
 * 触发；而那两页除了这里没有别的消费者。所以它不是可有可无的 UI 装饰 ——
 * 删掉/禁用它，那两页会整个失去实时推送，连底部的连接指示灯都不会亮。
 * 首页另有 tasks.js 也会 get()，不受影响，这条只对那两页成立。
 *
 * ⚠️ get() 之后必须在**同一个同步块**里立刻 socket.on(...)：socket.io 不重放
 * 错过的事件，而 connect 时那次快照是一次性的。理由完整写在 socket.js 的文件头。
 */
(function () {
    'use strict';

    // ready / failed 是**粘性终态**：进过之后不再接受降级回 running / idle。
    //
    // 这不是防御性编程，是一个真实存在的跨线程窗口：服务端的 connect handler 在
    // 请求线程里先读 snapshot() 再 emit 给本 sid，而后台解压线程同时在广播。交错
    // 顺序可以是「handler 读到 running(0.7) → 后台置 ready 并广播 → handler 才把
    // 那份**陈旧的** running 发出去」，于是客户端先收 ready 后收 running。
    // 不做粘性的话进度条会永远转下去 —— 解压已经结束，之后不会再有任何事件来纠正
    // 它（没有增量事件，也没有 REST 端点能补拿）。
    // 终态之间仍允许互相覆盖（failed 之后又来 ready 要认），所以守卫只挡非终态。
    let settled = false;

    function render(state) {
        const box = document.getElementById('statusBaseUnpack');
        const text = document.getElementById('statusBaseUnpackText');
        const prog = document.getElementById('statusBaseUnpackProgress');
        const bar = document.getElementById('statusBaseUnpackBar');
        if (!box || !text || !prog || !bar) return;

        const phase = state && state.phase;

        if (phase === 'running') {
            const percent = Math.round(Math.max(0, Math.min(1, state.fraction || 0)) * 100);
            box.hidden = false;
            box.classList.remove('statusbar-basemap--failed');
            box.title = '';
            text.textContent = t('js.base_unpack.running', { percent: percent });
            prog.hidden = false;
            bar.style.width = percent + '%';
            return;
        }

        if (phase === 'failed') {
            box.hidden = false;
            box.classList.add('statusbar-basemap--failed');
            text.textContent = t('js.base_unpack.failed');
            // 原因是用户唯一能据以行动的信息（多半是 assets/ 不可写）。
            box.title = t('js.base_unpack.failed_title', { error: state.error || '' });
            prog.hidden = true;
            return;
        }

        // idle / ready：整个元素收起来。ready 之后不留「已完成」的残迹 ——
        // 那是一次性的启动事项，长期占着状态栏没有信息量。
        box.hidden = true;
        box.classList.remove('statusbar-basemap--failed');
        box.title = '';
        prog.hidden = true;
    }

    function onProgress(state) {
        const phase = state && state.phase;
        const terminal = (phase === 'ready' || phase === 'failed');
        if (settled && !terminal) return;   // 陈旧的 running/idle，丢弃（见上）
        if (terminal) settled = true;
        render(state);
    }

    const socket = window.TerraSocket && window.TerraSocket.get();
    if (!socket) return;   // 没有 socket 的环境（库没加载）静默降级
    socket.on('base_unpack_progress', onProgress);
})();
