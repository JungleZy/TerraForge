/**
 * 任务时间流的响应式数据层（Vue 3 reactive）。
 *
 * 存在的理由：改造前 `activeTasks`（Map）只是个缓存，**不驱动渲染**——每次
 * 状态变化都要手动配对两件事：写 Map，再自己 `getElementById('task-'+key)`
 * 去改 DOM。这个配对在 tasks.js 里出现了 11 处，漏掉后半截就是「数据变了
 * 界面不动」，而且没有任何机制会报错。
 *
 * 现在只剩一件事：写这里。DOM 由 task_list.js 的 Vue 组件跟着变。
 *
 * ## 两个写入方
 *   - history.js `loadHistory()`  —— 翻页/筛选，整页替换（replaceAll）
 *   - tasks.js   socket 事件      —— 逐条增量（upsert / patch / remove）
 * 改造前这两条路各写各的 DOM，是「双写」的根源；现在汇到同一个 state.tasks。
 *
 * ## 为什么不用 ES module
 * 全站脚本是普通 <script> 顺序加载、共享全局作用域（index.html:418-422），
 * 没有构建步骤。这里用 IIFE 挂 window.TaskStore，与 TerraSocket / TerraTheme
 * 同一形态。IIFE 内的 const 不会和 history.js/tasks.js 的顶层 const 撞名
 * （那两个文件共享全局作用域，const 重名会直接 SyntaxError）。
 */
(function () {
    'use strict';

    if (!window.Vue) {
        console.error('[TaskStore] Vue 未加载 —— 任务列表将无法渲染');
        return;
    }
    const { reactive } = window.Vue;

    // 活动态：三者之外都是终态。与后端 TaskStatus 五态对齐。
    const LIVE_STATUSES = ['pending', 'running', 'paused'];

    const state = reactive({
        // 有序：后端按创建时间倒序返回，新任务 unshift 到头部。
        // 顺序即渲染顺序，不在渲染层再排一次。
        tasks: [],
        // 每秒自增，唯一作用是让「耗时」这类依赖 Date.now() 的 computed 失效
        // 重算。改造前这件事由 updateTimeDisplay 每秒遍历 DOM 写 textContent
        // 完成（tasks.js:828-844）。
        tick: 0,
        // 列表拉取失败的提示文案（''=无错）。改造前是直接往
        // #historyTableBody 写 innerHTML，Vue 接管容器后那么写会被下次
        // patch 覆盖掉。
        loadError: '',
        // 搜索框的当前值。客户端过滤做成**派生视图**而不是替换 tasks——
        // 改造前 filterTasks 是 renderHistoryTable(filtered)，等于用过滤结果
        // 覆盖了数据本身，搜索期间到达的 socket 增量会写进一个「已经被过滤集
        // 顶掉」的数组里。
        searchTerm: '',
        // 活动任务索引（key -> task），与 tasks 是**两个不同的集合**：
        //   tasks  = 时间流，/api/history_all 分页拉的当前一页，负责渲染
        //   active = 四条管线 ?status=active 拉的**全量**活动任务，负责底部
        //            状态栏的聚合读数（进行中数量 + 汇总进度）
        // 一个任务可能只在其中一边：跑在第 3 页的活动任务不在 tasks 里，
        // 但必须计进状态栏。改造前这是 tasks.js 的裸 Map `activeTasks`，
        // 与 DOM 各写各的；现在两个集合都在 store 里，由 commit() 一次写全。
        active: {},
    });

    // key -> state.tasks 里那个**响应式代理对象**的引用。
    // 只为 O(1) 定位；真相始终是 state.tasks 这个数组。
    const index = new Map();

    function keyOf(task) {
        return task._key || `${task.task_type}:${task.id}`;
    }

    function reindex() {
        index.clear();
        state.tasks.forEach(t => index.set(t._key, t));
    }

    /** 整页替换（loadHistory）。会丢掉本地增量字段之外的一切，符合「翻页即重拉」语义。 */
    function replaceAll(list) {
        const rows = (list || []).map(t => {
            const row = Object.assign({}, t);
            row._key = keyOf(row);
            return row;
        });
        state.tasks.splice(0, state.tasks.length, ...rows);
        reindex();
        state.loadError = '';    // 拿到数据就算恢复
    }

    function setLoadError(msg) {
        state.loadError = msg || '';
    }

    function setSearchTerm(term) {
        state.searchTerm = term || '';
    }

    /** 搜索过滤后的可见集合。空搜索词直接返回全量，不白建数组。 */
    function visibleTasks() {
        const term = state.searchTerm.trim().toLowerCase();
        if (!term) return state.tasks;
        return state.tasks.filter(task =>
            String(task.name || '').toLowerCase().includes(term) ||
            String(task.id).includes(term)
        );
    }

    function get(key) {
        return index.get(key);
    }

    function has(key) {
        return index.has(key);
    }

    /**
     * 增量合并。存在则就地 Object.assign（响应式，视图自动跟），
     * 不存在则按 prepend 策略插到头部。
     *
     * 返回被写入的那个响应式对象，调用方可以继续改它的字段。
     *
     * ⚠️ index 里存的必须是**数组里那个 reactive proxy**，不能是 unshift
     * 进去的原始对象。reactive 数组会把写入的普通对象包一层代理，两者不是
     * 同一个引用；改原始对象不经过代理，依赖收集拿不到通知，视图不更新。
     * 这个坑实测过：连发三次推送只有第一次（走 unshift 建行）生效，后续
     * commit 全部写进了那个游离的原始对象。
     */
    function upsert(key, patch, { prepend = true } = {}) {
        const existing = index.get(key);
        if (existing) {
            Object.assign(existing, patch);
            return existing;
        }
        if (!prepend) return null;
        state.tasks.unshift(Object.assign({}, patch, { _key: key }));
        const proxy = state.tasks[0];
        index.set(key, proxy);
        return proxy;
    }

    /** 只改已存在的行；行不在流里（被分页窗口挤掉了）时什么都不做。 */
    function patch(key, changes) {
        const existing = index.get(key);
        if (!existing) return null;
        Object.assign(existing, changes);
        return existing;
    }

    /** 从流里移除一行（dismissTask 的「移除」，不碰后端）。同时出活动集。 */
    function remove(key) {
        const i = state.tasks.findIndex(t => t._key === key);
        if (i >= 0) state.tasks.splice(i, 1);
        index.delete(key);
        delete state.active[key];
    }

    function clear() {
        state.tasks.splice(0, state.tasks.length);
        index.clear();
    }

    // --- 活动任务集（状态栏聚合） -------------------------------------------

    /** loadActiveTasks 的四路合并结果，整体替换。 */
    function setActive(list) {
        const next = {};
        (list || []).forEach(task => {
            const key = keyOf(task);
            next[key] = Object.assign({}, task, { _key: key });
        });
        state.active = next;
    }

    function getActive(key) {
        return state.active[key];
    }

    /** 任务进入终态后立刻出活动集，但**留在时间流里**（失败行要能看见）。 */
    function dropActive(key) {
        delete state.active[key];
    }

    /**
     * 一次写全两个集合 —— socket 事件的唯一入口。
     *
     * 时间流里没有这个 key（任务落在别的分页窗口）时只更新活动集：
     * 状态栏读数照样准，而那一行本来就不显示。改造前这是「更新 Map 但
     * getElementById 拿不到行就跳过」的同一语义,只是不再需要调用方自己判断。
     *
     * ⚠️ 这里**故意不做**「终态不可被活动态推送覆盖」的钳制。试过一版,是错的:
     *
     * 现象确实存在 —— 取消任务后 0.5s 内会到一发 status='running' 的
     * task_progress,把「已取消」翻回「运行中」且永久卡住(库里已是 cancelled,
     * _complete_task 看到就直接 return,再也不发任何终态事件)。
     *
     * 但根因在后端:下载循环要跑到当前批次边界才停,期间 progress_callback
     * 照发,而载荷里的 status 取自内存对象 —— cancel/pause 只改库不碰它。
     * 已在 task_manager.py 的 progress_callback 里按 stop flag 掐掉这些广播。
     *
     * 前端不能替后端兜底:**重启**一个已取消/已失败的任务时,后端发的那发
     * 权威 running 广播(task_manager.py:456,status 取自刚查的库行)和迟到的
     * 推送长得一模一样,任何基于「这个 key 曾经进过终态」的钳制都会把重启
     * 一起拦掉 —— 用户看到的是「点了启动没反应」。
     */
    function commit(key, patch) {
        const row = index.get(key);
        if (row) Object.assign(row, patch);
        const act = state.active[key];
        if (act) {
            Object.assign(act, patch);
        } else if (patch && isLive(patch)) {
            // 新出现的活动任务（首个 task_progress 早于下一次 loadActiveTasks）
            state.active[key] = Object.assign({}, row || {}, patch, { _key: key });
        }
        return row;
    }

    function isLive(task) {
        return LIVE_STATUSES.includes(task && task.status);
    }

    /** 进行中的任务（状态栏聚合读数用）。失败任务留在集合里但不算活动。 */
    function liveTasks() {
        return Object.values(state.active).filter(isLive);
    }

    /** 每秒一次，驱动耗时类 computed 重算。 */
    function bumpTick() {
        state.tick = (state.tick + 1) % 1000000;
    }

    window.TaskStore = {
        state,
        LIVE_STATUSES,
        keyOf,
        replaceAll,
        setLoadError,
        setSearchTerm,
        visibleTasks,
        get,
        has,
        setActive,
        getActive,
        dropActive,
        commit,
        upsert,
        patch,
        remove,
        clear,
        isLive,
        liveTasks,
        bumpTick,
    };
})();
