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
 * 全站脚本是普通 <script> 顺序加载、共享全局作用域（templates/index.html 的
 * extra_js 块），没有构建步骤。这里用 IIFE 挂 window.TaskStore，与
 * TerraSocket / TerraTheme
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

    // 「还在推进」的状态：进度条、耗时/ETA、状态栏汇总都只认这一档。
    // retrying（补漏重跑）在内 —— 它与首次下载一样在拉瓦片、一样有进度。
    //
    // pending_decision **刻意不在**内：它不再推进，等的是用户点「补漏」还是
    // 「接受并导出」。算进来的后果是状态栏的「N 个活动任务 … X%」永远不归零
    // （没有任何后续推送会把它挪出这个集合），而行上会显示一根静止的进度条
    // 与一个每秒自增的「已运行」—— 两者都在暗示「它还在跑」。
    const LIVE_STATUSES = ['pending', 'running', 'paused', 'retrying'];

    // 「还在系统手里」：后端 ACTIVE_TASK_STATES（src/contracts/outcome.py）的
    // 镜像，比 LIVE_STATUSES 多一个 pending_decision —— 它占着产物目录、
    // 占着缓存引用，四路 ?status=active 的响应里会有它，前端的白名单不能把它
    // 滤掉（滤掉就是「任务不见了」，而它正等着用户做决定）。
    // 全站唯一一份：task_center.js 的 loadActiveTasks 直接读这个数组，
    // 不在调用处再抄一遍状态字面量。
    const ACTIVE_STATUSES = ['pending', 'running', 'paused', 'retrying', 'pending_decision'];

    // 「此刻真在占资源」：running 与 retrying。两处消费者 ——
    // calculateTimeInfo 要据此决定是否给累计时长叠上当前这一段，
    // 状态栏的「M 运行中」要据此计数。写成函数而不是让调用方
    // `status === 'running' || status === 'retrying'`：那个二元判断在补漏
    // 上线前只有一半，两个调用点各漏一次就是两个 bug。
    const RUNNING_STATUSES = ['running', 'retrying'];

    // --- 缺口相关的三张状态集（§13-3）-------------------------------------
    // 三张表都是**后端规则的镜像**，前端只用它们决定「渲染哪颗按钮」；
    // 能不能真的执行由服务端判定，被拒时把它的理由原文透出（见
    // task_center.js 的 refillTask / acceptTaskGaps）。前端不复制那套状态机，
    // 只复制「该给用户看见哪条出路」。
    //
    // 全部放在本文件而不是各自的消费者里：状态字面量一旦散落，加一个状态就要
    // 满仓 grep，而漏掉一处的表现是「按钮不出现」—— 没有任何报错。

    // 「产物带洞」：后端 TaskState.has_gaps 的镜像。行上与详情里的缺口读数、
    // 常驻缺口徽章都看它。
    const GAP_STATUSES = ['pending_decision', 'completed_with_gaps'];

    // 允许「补漏」：后端 refill_task 的入口条件镜像。failed 在内 —— 整条任务
    // 失败后仍可能只差几十块可重试的瓦片，不给这条出路等于让用户重跑整个任务。
    const REFILLABLE_STATUSES = ['completed_with_gaps', 'pending_decision', 'failed'];

    // 「产出可用（可能带洞）」：后端 TaskState.is_successful 的镜像。
    // 预览与「导出 MBTiles」看它 —— completed_with_gaps 的产物是可用的，
    // 把它排除掉就等于让「接受缺口」这个决定毫无意义。
    const SUCCESSFUL_STATUSES = ['completed', 'completed_with_gaps'];

    const state = reactive({
        // 有序：后端按创建时间倒序返回，新任务 unshift 到头部。
        // 顺序即渲染顺序，不在渲染层再排一次。
        tasks: [],
        // 每秒自增，唯一作用是让「耗时」这类依赖 Date.now() 的 computed 失效
        // 重算。改造前这件事由 tasks.js 的 updateTimeDisplay 每秒遍历 DOM 写
        // textContent 完成。
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

    // 整页替换时要**搬过去**的客户端增强字段。
    //
    // /api/history_all 回的是库里那一行，里面没有 gap_summary —— 那是
    // task_center.js 的 ensureGapSummary 打过一次 GET /api/tasks/<id>/gaps
    // 之后挂上来的分档明细。整行替换会把它抹掉，而行组件重新拉取的三个触发点
    // （mounted 与 task.status / task.gap_tiles 两条 watch）在「同一个 key
    // 原地换了个对象、状态与缺块数都没变」时**一个都不会响**：keyed diff 复用
    // 组件实例所以不 mount，两条 watch 求值结果不变所以不触发。于是那一行永久
    // 停在「正在读取缺块明细…」，而接口其实早就 200 回过了（实测两个任务都这样）。
    //
    // `gap_summary_error` 同理，而且**必须**一起搬：它是「那一次没拉回来」的
    // 唯一记录，也是行上错误态与「重试」按钮的开关。不搬的话整页替换会把行打回
    // 「正在读取缺块明细…」，而上面那三个触发点在这种替换下同样一个都不响 ——
    // 等于把刚修掉的那个永久转圈原样放回来，只是换了条路径进来。
    //
    // 判据是「缺块数没变」而不是无脑搬运：数变了说明补漏跑过（或后端重算过），
    // 旧明细已经过期，这时**必须**让它消失 —— 消失才会让 task.gap_tiles 那条
    // watch 拿到新值、重新拉一份准的。同一个判据也让过期的失败记录跟着消失。
    //
    // 只搬这两个，不搬 stage_text / tiling_* 这类阶段字段：那些没有
    // 「还有效吗」的判据，搬过去就是一段过期的阶段文字永久顶掉计数
    // （task_center.js updateTaskStageText 的注释记的就是这个失败模式）。
    function carryClientState(prev, next) {
        if (!prev || next.gap_summary) return;
        if (prev.gap_tiles !== next.gap_tiles) return;
        // 明细优先：两者不会同时有效（ensureGapSummary 成功写明细、失败写错误，
        // 且起手先清错误），这里按同样的优先级还原。
        if (prev.gap_summary) next.gap_summary = prev.gap_summary;
        else if (prev.gap_summary_error) next.gap_summary_error = prev.gap_summary_error;
    }

    /** 整页替换（loadHistory）。除 carryClientState 搬运的那几个字段外一切以响应为准。 */
    function replaceAll(list) {
        const rows = (list || []).map(t => {
            const row = Object.assign({}, t);
            row._key = keyOf(row);
            // index 此刻还是**替换前**那一批，正好用来取上一轮的增强字段。
            carryClientState(index.get(row._key), row);
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

    /** 从流里移除一行（deleteTask 删成功后同步界面用）。同时出活动集。 */
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
     * （下面这段推理写于「取消任务」还在的时候,原文照录不改。该功能已下线,见
     *   models/task.py 的 TaskStatus。结论仍成立,只是触发者换了人:置停止标志的
     *   现在是暂停与删除,迟到广播的形态一模一样。第二条论据「重启」则已失效 ——
     *   start_task 现在只收 pending / paused,终态任务不能重启。）
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
     * 权威 running 广播(task_manager.start_task 末尾那发 task_progress,status
     * 取自刚查的库行)和迟到的
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

    /** 是否正在占资源（running / retrying）。 */
    function isRunning(task) {
        return RUNNING_STATUSES.includes(task && task.status);
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
        ACTIVE_STATUSES,
        RUNNING_STATUSES,
        GAP_STATUSES,
        REFILLABLE_STATUSES,
        SUCCESSFUL_STATUSES,
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
        isRunning,
        liveTasks,
        bumpTick,
    };
})();
