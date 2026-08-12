/**
 * 首页专属的任务胶水：底部状态栏的两个读数。
 *
 * ## 这个文件为什么这么小
 *
 * 它曾经是 749 行、装着整条实时链（socket 监听、四路活动列表拉取、进度合并、
 * 终态处理、耗时/速度格式化、启动/暂停/恢复）。但它挂在 index.html 的
 * extra_js 里 —— **首页专属**。于是独立页 /history 上，同一批任务没有按钮、
 * 没有实时更新、没有耗时、没有速度：task_list.js 的行组件用
 * `typeof startTask === 'function'` 探测，探不到就不渲染。
 *
 * 那不是「/history 是个简化版」，那是同一份数据由两套能力呈现。§6.1 把它定为
 * phase-3 的闸门，修法是**抽取**：与首页那张地图无关的一切搬进
 * static/js/task_center.js，由 base.html 全局加载（/config 覆盖那个 block 成
 * 空，配置页仍不白付）。
 *
 * 留在这里的只有一类东西：**只有首页存在的 DOM**。底部状态栏的活动任务聚合
 * （#statusTasksText / #statusTasksProgress）与最近事件（#statusEventText）
 * 都由 index.html 的 statusbar block 提供，其它页面根本没有这些元素。
 *
 * task_center.js 通过 `typeof updateStatusTasks === 'function'` 调这两个函数
 * （包在它自己的 refreshStatusBar / emitStatusEvent 里）—— 裸调会在 /history
 * 上抛 ReferenceError，而抛出点在 socket 处理器里，整条实时链会静默断掉。
 *
 * ⚠️ 本文件**不许**再定义任何 task_center.js 已有的名字。两个文件共享全局
 * 作用域：同名 `function` 声明按加载顺序静默互相覆盖，同名 `const` / `let`
 * 直接 SyntaxError 打挂整页。
 */

// --- 底部状态栏：活动任务聚合 + 最近事件 ----------------------------------------
// 状态栏元素只在首页存在（#statusTasksText 等），其它页不加载本文件，
// 这里仍全部做 null 守卫，避免未来被其它页引入时报错。

// 活动任务读数：进行中（pending/running/paused/retrying）任务数 + 汇总进度条。
// failed 行留在活动集里等用户移除，但不算「活动」；pending_decision 也不算 ——
// 它不再推进，算进去会让「N 个活动任务 … X%」永远不归零（详见
// task_center.js 的 handleTaskGapDecision）。判据全部交给 TaskStore.liveTasks()，
// 本文件不再抄一份状态清单。
// task_progress 每个事件都调这里：文本/宽度算出来后先比对再写——无条件
// 写 textContent/style.width 每次都会触发 DOM 变更（高频事件下白付 layout）。
function updateStatusTasks() {
    const textEl = document.getElementById('statusTasksText');
    if (!textEl) return;
    const barWrap = document.getElementById('statusTasksProgress');
    const barFill = document.getElementById('statusTasksBar');
    const live = window.TaskStore ? window.TaskStore.liveTasks() : [];
    if (live.length === 0) {
        const idle = t('js.tasks.status_bar.idle');
        if (textEl.textContent !== idle) {
            textEl.textContent = idle;
        }
        if (barWrap && !barWrap.hidden) {
            barWrap.hidden = true;
        }
        return;
    }
    // 「运行中」把补漏重跑（retrying）也算进去：它与首次下载一样在占着网络
    // 连接与 CPU，状态栏读数是给用户判断「现在能不能再开一个任务」的，
    // 把 retrying 排除等于少报一份真实负载。判据走 store 的 isRunning，
    // 不在这里写状态字面量。
    const running = live.filter(t => window.TaskStore.isRunning(t)).length;
    let total = 0;
    let done = 0;
    live.forEach(t => {
        const itemTotal = t.total_items || 0;
        total += itemTotal;
        // 切片中的任务（本地地形）：它的条目计数是**上传的文件数**，上传秒级
        // 结束就写满，照着算等于整个切片期间这条恒计 100%。改按切片百分比
        // 折算，但仍用它自己的条目数当权重 —— 换成「0~100」的百分比单位会让
        // 一个 3 文件的切片任务在汇总里盖过一个几万瓦片的下载任务。
        done += t.tiling_progress != null
            ? itemTotal * t.tiling_progress / 100
            : (t.downloaded_items || 0);
    });
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    const text = t('js.tasks.status_bar.active', {n: live.length, running: running, pct: pct});
    if (textEl.textContent !== text) {
        textEl.textContent = text;
    }
    if (barWrap && barFill) {
        if (barWrap.hidden) {
            barWrap.hidden = false;
        }
        const width = pct + '%';
        if (barFill.style.width !== width) {
            barFill.style.width = width;
        }
    }
}

// 最近事件单行读数：任务完成/失败/拼接/复制阶段、以及缺口待决的文字心跳。
// 这些 socket 事件原本只 console.log，状态栏是唯一消费者。
// 写的是胶囊里的文字 span 而不是 #statusEvent 本身：胶囊第一个子节点是图标
// SVG，往胶囊写 textContent 会连图标一起抹掉。空/非空的隐藏判定也跟着挪到了
// 这个 span 上（style.css 的 `.statusbar-event:has(...:empty)`）。
function pushStatusEvent(msg) {
    const el = document.getElementById('statusEventText');
    if (!el) return;
    el.textContent = msg;
}

/**
 * 首页的任务入口（index.html 的 boot 块调）。
 *
 * 实时链本身已经由 task_center.js 在解析期自举完毕（socket.io 不重放错过的
 * 事件，接线不能等 DOMContentLoaded —— 见那个文件的说明）。这里调
 * initTaskCenter() 只是兜住「加载顺序被改动」的情形，它幂等；随后把状态栏
 * 两个读数刷成首屏值 —— 自举那一遍跑在本文件之前，那时 updateStatusTasks
 * 还不存在（refreshStatusBar 的 typeof 守卫会跳过它）。
 */
function initTasks() {
    if (typeof initTaskCenter === 'function') initTaskCenter();
    updateStatusTasks();
}
