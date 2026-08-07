/**
 * 任务时间流的 Vue 渲染层（Vue 3 global build，无构建步骤）。
 *
 * 挂载到 #historyTableBody，接管时间流里每一行的渲染。改造前这件事分散在
 * 两个文件、8 个函数里：history.js 的 createTaskRow 拼模板字符串，
 * tasks.js 的 rebuildStreamRow（outerHTML 整行替换）/ updateTaskProgressPartial
 * （querySelector 逐节点写）/ prependStreamRow（insertAdjacentHTML）各写一套。
 * 现在只有这一份 template，数据从 TaskStore 来。
 *
 * ## DOM 结构是契约，不是实现细节
 * 下面这份 markup 与改造前 createTaskRow 的输出**逐节点等价**，改动任何
 * 类名 / 标签名 / 嵌套层级都会打穿 tests/test_css_contract.py 的层叠模型
 * （它按 `#historyTableBody > .task-row > .task-line1 > .task-dot` 这样的
 * 祖先链算最终样式，且 `.task-name` 必须是 button、`.task-id` 等必须是
 * span）。tests/test_tasks_js_contract.py 则直接 grep 本文件的 template。
 *
 * ## 转义
 * 全部走 `{{ }}` 插值，Vue 自动 HTML 转义 —— 改造前需要在 5 处手写
 * escapeHtml(task.name) / escapeHtml(historyMetaText(task))，漏一处就是
 * 一个 XSS 注入面。**本文件禁止出现 v-html**，契约测试钉住这一条：
 * error_message 是后端异常的字符串化结果（URL、路径、第三方库报错原文都
 * 可能在里面），任何时候都不能当 HTML 解析。
 *
 * ## 特性探测
 * 独立页 /history 不加载 tasks.js，那里没有 startTask 等动作函数，也没有
 * map.js 的 previewTask。与改造前一致，用 typeof 探测决定按钮是否渲染。
 */
(function () {
    'use strict';

    if (!window.Vue || !window.TaskStore) return;

    const { createApp, computed } = window.Vue;
    const store = window.TaskStore;

    // 速度多久没刷新就算「停滞」，显示 0 B/s。
    // 下界：后端 emit 节流最慢的一条是 DEM / 等高线的 1s，留 5 倍余量，
    // 正常下载不会误判。上界：再长用户就会盯着一个假速度发呆。
    const SPEED_STALE_MS = 5000;

    // 拉取失败提示。改造前是 loadHistory 的 catch 直接往 #historyTableBody
    // 写 innerHTML，Vue 接管容器后那样写会被下次 patch 抹掉。
    const ERROR_TEMPLATE = `
        <div class="text-center text-danger task-load-error" style="padding: 1.5rem 1rem" v-if="loadError">{{ loadError }}</div>`;

    // 空态图标与「暂无任务」提示。与改造前 renderHistoryTable 的空态等价。
    const EMPTY_TEMPLATE = `
        <div class="task-empty" v-if="!loadError && !tasks.length">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity: 0.3; margin-bottom: 1rem;">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <p style="margin: 0;">{{ t('js.history.empty') }}</p>
        </div>`;

    // 单行。v-for 的 :key 用 `${task_type}:${id}`——它在四条管线的 UNION 里
    // 唯一，且正是 DOM id 的取值。有了 keyed diff，改造前那套「插入前先
    // getElementById 查重、命中就改走 rebuild」的防重复逻辑在结构上不再需要。
    const ROW_TEMPLATE = `
        <div class="task-row" :class="'status-' + task.status" :id="'task-' + rowKey">
            <div class="task-line1">
                <span class="task-dot" aria-hidden="true"></span>
                <button type="button" class="task-name" @click="viewDetails" :title="t('js.history.row.view_details')">{{ task.name }}</button>
                <span class="task-id">#{{ rowKey }}</span>
                <span class="task-meta">{{ metaText }}</span>
                <span class="task-status-text">{{ statusText }}</span>
                <span class="task-time progress-detail">{{ timeText }}</span>
                <div class="btn-group btn-group-sm">
                    <button v-if="hasTaskActions && isLive && supportsPauseResume && task.status === 'pending'"
                            class="btn btn-icon btn-success" @click="act('startTask')"
                            :title="t('js.history.action.start')" :aria-label="t('js.history.action.start')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="5 3 19 12 5 21 5 3"></polygon>
                        </svg>
                    </button>
                    <button v-if="hasTaskActions && isLive && supportsPauseResume && task.status === 'running'"
                            class="btn btn-icon btn-warning" @click="act('pauseTask')"
                            :title="t('js.history.action.pause')" :aria-label="t('js.history.action.pause')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="6" y="4" width="4" height="16"></rect>
                            <rect x="14" y="4" width="4" height="16"></rect>
                        </svg>
                    </button>
                    <button v-if="hasTaskActions && isLive && supportsPauseResume && task.status === 'paused'"
                            class="btn btn-icon btn-success" @click="act('resumeTask')"
                            :title="t('js.history.action.resume')" :aria-label="t('js.history.action.resume')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="5 3 19 12 5 21 5 3"></polygon>
                        </svg>
                    </button>
                    <button v-if="hasTaskActions && isLive && task.status !== 'failed'"
                            class="btn btn-icon btn-danger" @click="act('cancelTask')"
                            :title="t('js.history.action.cancel')" :aria-label="t('js.history.action.cancel')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"></line>
                            <line x1="6" y1="6" x2="18" y2="18"></line>
                        </svg>
                    </button>
                    <button v-if="canPreview && task.status === 'completed'"
                            class="btn btn-icon btn-sm btn-success" @click="preview"
                            :title="t('js.history.action.preview')" :aria-label="t('js.history.action.preview')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                            <circle cx="12" cy="12" r="3"></circle>
                        </svg>
                    </button>
                    <button class="btn btn-icon btn-sm btn-danger" @click="remove"
                            :title="t('js.history.action.delete')" :aria-label="t('js.history.action.delete')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                    <button v-if="hasTaskActions && task.status === 'failed'"
                            class="btn btn-icon btn-secondary" @click="act('dismissTask')"
                            :title="t('js.history.action.dismiss_title')" :aria-label="t('js.history.action.dismiss_label')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"></line>
                            <line x1="6" y1="6" x2="18" y2="18"></line>
                        </svg>
                    </button>
                </div>
            </div>
            <!-- 不挂 role="alert"/"status"：这是**历史失败记录**的既存状态，不是刚发生的
                 事件，而列表每次渲染都会把它整批插进 DOM —— 一屏 N 条失败任务就等于 N 条
                 assertive 公告同时抢读，打断读屏用户正在听的任何内容。失败**事件**的即时
                 通报由 handleTaskFailed 的 toast 承担，那才是「刚刚发生」。 -->
            <div class="task-error" v-if="isFailed">{{ errorText }}</div>
            <div class="task-progress-line" v-else-if="isLive">
                <div class="task-progress">
                    <div class="progress-bar" :class="'bg-' + statusColor" role="progressbar"
                         :style="{ width: progress + '%' }"
                         :aria-valuenow="progress"
                         aria-valuemin="0"
                         aria-valuemax="100"></div>
                </div>
                <span class="task-pct" aria-hidden="true">{{ progress }}%</span>
                <span class="task-count progress-detail" v-if="stageText">{{ stageText }}</span>
                <span class="task-count progress-detail" v-else>{{ countText }}<span v-if="failedItems > 0" style="color: var(--color-danger);"> | {{ failedText }}</span></span>
                <span class="task-speed progress-detail" v-if="speedText">{{ speedText }}</span>
            </div>
            <template v-else>
                <div class="task-line2">{{ summaryText }}{{ bboxText }}</div>
                <div class="task-line2 task-tiling-line" v-if="task.tiling_text">{{ task.tiling_text }}</div>
            </template>
        </div>`;

    const TaskRow = {
        name: 'TaskRow',
        props: { task: { type: Object, required: true } },
        template: ROW_TEMPLATE,
        computed: {
            rowKey() {
                return this.task._key || `${this.task.task_type}:${this.task.id}`;
            },
            isLive() {
                return store.LIVE_STATUSES.includes(this.task.status);
            },
            isFailed() {
                return this.task.status === 'failed';
            },
            // 独立页 /history 不加载 tasks.js：那里只给预览/详情/删除。
            hasTaskActions() {
                return typeof startTask === 'function';
            },
            // 本地地形任务没有暂停/恢复语义（后端状态机不支持）。
            supportsPauseResume() {
                return this.task.task_type !== 'local_terrain';
            },
            // 预览只在首页覆盖面板里有意义（主视图在旁边），独立页没有主视图。
            canPreview() {
                return typeof previewTask === 'function';
            },
            total() {
                const t = this.task;
                return t.total_items != null ? t.total_items : (t.total || 0);
            },
            downloaded() {
                const t = this.task;
                return t.downloaded_items != null ? t.downloaded_items : (t.downloaded || 0);
            },
            failedItems() {
                return this.task.failed_items || 0;
            },
            itemLabel() {
                const t = this.task;
                return t.items_label
                    || ((t.task_type === 'map' || t.task_type === 'contour')
                        ? this.t('js.history.unit.tile') : this.t('js.history.unit.file'));
            },
            progress() {
                return this.total > 0 ? Math.round((this.downloaded / this.total) * 100) : 0;
            },
            statusColor() {
                return getStatusColor(this.task.status);
            },
            statusText() {
                return getStatusText(this.task.status);
            },
            metaText() {
                return historyMetaText(this.task);
            },
            errorText() {
                return this.task.error_message || this.t('js.history.unknown_error');
            },
            // tiling_text 优先于 stage_text：本地地形任务切片期间仍是 running，
            // 进度条按上传数算、一上传完就写满，行上必须显示切片进度才看得出还在跑。
            stageText() {
                return this.task.tiling_text || this.task.stage_text || '';
            },
            countText() {
                return this.t('js.history.row.count', {
                    verb: this.task.progress_verb || this.t('js.history.progress_verb.downloaded'),
                    downloaded: this.downloaded,
                    total: this.total,
                    unit: this.itemLabel,
                });
            },
            failedText() {
                return this.t('js.history.row.failed', { count: this.failedItems });
            },
            bboxText() {
                const t = this.task;
                if (t.north == null || t.south == null || t.east == null || t.west == null) return '';
                return ' · ' + this.t('js.history.row.bbox', {
                    north: t.north.toFixed(2), south: t.south.toFixed(2),
                    east: t.east.toFixed(2), west: t.west.toFixed(2),
                });
            },
            summaryText() {
                return this.t('js.history.row.summary', {
                    status: this.statusText,
                    downloaded: this.downloaded,
                    total: this.total,
                    unit: this.itemLabel,
                });
            },
            // 非终态显示耗时、终态显示创建时间短日期。
            // 依赖 store.state.tick：它每秒自增一次，是「耗时每秒刷新」的驱动源
            // （改造前是 updateTimeDisplay 每秒遍历 DOM 写 textContent）。
            timeText() {
                if (!this.isLive) return formatShortDate(this.task.created_at);
                void store.state.tick;      // 建立依赖，勿删
                if (typeof calculateTimeInfo !== 'function') return '—';
                const info = calculateTimeInfo({
                    started_at: this.task.started_at,
                    status: this.task.status,
                    total_running_seconds: this.task.total_running_seconds,
                    downloaded_items: this.downloaded,
                    total_items: this.total,
                });
                if (!info.show) return '—';
                return [
                    info.elapsed ? this.t('js.history.row.elapsed', { time: info.elapsed }) : '',
                    info.estimated ? this.t('js.history.row.estimated', { time: info.estimated }) : '',
                ].filter(Boolean).join(' · ') || '—';
            },
            // 下载速度。只在**下载阶段**出现，三种情况各自不同：
            //   - stageText 有值 = 已进入拼接 / 复制 / 切片，没有网络下载，隐藏；
            //   - speed_at 为空 = 后端这一发没带速度（等高线渲染阶段），隐藏；
            //   - speed_at 过期 = 推送停了但任务还在 running（网断了却没判失败），
            //     显示 0 B/s。不这么做界面会永远冻在最后那个 2.3 MB/s，看着像还在跑。
            speedText() {
                if (this.task.status !== 'running') return '';
                if (this.stageText) return '';
                const at = this.task.speed_at;
                if (!at || typeof formatSpeed !== 'function') return '';
                void store.state.tick;      // 每秒心跳，过期归零靠它触发，勿删
                const stale = Date.now() - at > SPEED_STALE_MS;
                const bps = stale ? 0 : (this.task.download_speed_bps || 0);
                return this.t('js.history.row.speed', { speed: formatSpeed(bps) });
            },
        },
        methods: {
            // 动作函数是全局的（tasks.js 定义，被 map.js/panels.js 也引用），
            // 这里按名字转发而不是直接绑定：独立页上它们不存在，v-if 已经挡住
            // 渲染，这层 typeof 是第二道防线。
            act(fnName) {
                const fn = window[fnName];
                if (typeof fn === 'function') fn(this.task.id, this.task.task_type);
            },
            viewDetails() {
                if (typeof viewTaskDetails === 'function') viewTaskDetails(this.task.id, this.task.task_type);
            },
            preview() {
                if (typeof previewHistoryTask === 'function') previewHistoryTask(this.task.id, this.task.task_type);
            },
            remove() {
                if (typeof deleteTask === 'function') deleteTask(this.task.id, this.task.task_type);
            },
        },
    };

    const TaskList = {
        name: 'TaskList',
        components: { TaskRow },
        template: `${ERROR_TEMPLATE}${EMPTY_TEMPLATE}
        <task-row v-for="task in tasks" :key="task._key" :task="task"></task-row>`,
        computed: {
            // 搜索是**派生视图**：store.state.tasks 始终是完整真相，搜索期间
            // 到达的 socket 增量照样写得进去。改造前 filterTasks 是拿过滤结果
            // 覆盖整个列表，那期间来的实时更新会写进一个已被顶掉的数组。
            tasks() {
                return store.visibleTasks();
            },
            loadError() {
                return store.state.loadError;
            },
        },
    };

    let app = null;

    /**
     * 挂到 #historyTableBody。首页与独立页 /history 都有这个容器，
     * 页面初始化时各调一次；重复调用是幂等的。
     */
    function mountTaskList() {
        const el = document.getElementById('historyTableBody');
        if (!el || app) return app;
        app = createApp(TaskList);
        // template 里直接调 t()/getStatusColor 等全局函数：Vue 模板表达式的
        // 作用域是组件实例，够不到 window 上的自由变量，必须显式挂上来。
        app.config.globalProperties.t = window.t;
        app.mount(el);
        return app;
    }

    window.TaskList = { mount: mountTaskList, TaskRow, TaskList };
})();
