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

    // 值得给一颗导出按钮的管线。真值来源是后端
    // `artifact_export._PIPELINE_TILE_LAYOUT`（哪条管线有松散瓦片金字塔），
    // tests/test_tasks_js_contract.py 逐字对表 —— 那边加一条管线而这里不加，
    // 表现是「按钮不出现」，没有任何报错。
    //
    // 为什么不是「所有成功的任务都给按钮」：dem / local_terrain 一件产物都不
    // 登记（task_manager._register_artifacts 里 pipeline 写死 'map'），它们的
    // 格式清单恒为空，按钮点下去只能弹一句「没有可导出的产物」。
    // 代价记在这里：第三方插件管线若登记了 GEOTIFF 产物，它确实导得出 gpkg，
    // 但行上不会有按钮 —— 要修得在页面引导数据里带上后端那份管线表。
    const EXPORTABLE_PIPELINES = ['map', 'contour'];

    // 拉取失败提示 + 重载入口。改造前是 loadHistory 的 catch 直接往
    // #historyTableBody 写 innerHTML，Vue 接管容器后那样写会被下次 patch 抹掉。
    //
    // 改造前这条分支只有一句红字：列表拉失败之后**页面上没有任何出路**，
    // 用户唯一能做的是按 F5（连带丢掉筛选、页码与已展开的面板）。
    //
    // ⚠️ 这颗按钮重发的是**列表那一次 GET**，与任务状态机没有一点关系。键叫
    // `reload_list`、文案是「重新加载列表」，刻意不带「重试」二字：任务级重试
    // 被否过两次（三个 manager 的 start_task 只收 pending/paused；失败任务的
    // 设计是删掉重建），键里出现 retry 早晚会有人把它当成「重试这个任务」的
    // 现成入口往行上搬。
    // 消息文本直接放在这个 div 里、按钮跟在后面（`d-block mx-auto` 让它自己
    // 占一行并居中）：**不套内层 <div>**。tests/test_css_contract.py 的
    // `_history_error_div` 从这段模板里解析出「加载失败提示」那个文本节点去
    // 算对比度，它按「恰好一个 <div>」认路 —— 多一层就把那条 WCAG 断言变成
    // 「本测试已失效」，而那正是当初实测逃逸出 Critical 的那一条。
    const ERROR_TEMPLATE = `
        <div class="text-danger task-load-error" v-if="loadError">
            {{ loadError }}
            <button type="button" class="btn btn-sm btn-outline-secondary d-block mx-auto mt-2"
                    @click="reloadList($event)"
                    :title="t('js.history.action.reload_list')">{{ t('js.history.action.reload_list') }}</button>
        </div>`;

    // 空态图标与「暂无任务」提示。与改造前 renderHistoryTable 的空态等价。
    const EMPTY_TEMPLATE = `
        <div class="task-empty" v-if="!loadError && !tasks.length">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <p>{{ t('js.history.empty') }}</p>
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
                <!-- 缺口徽章：**常驻**，不是决策期间的临时提示。
                     completed_with_gaps 的产物可用，用户会拿去做后续处理；
                     几个月后回到列表时「已完成」与「已完成（有缺口）」在扫视中
                     必须能一眼分开，而两者的差别是「缺了 N 块瓦片」。
                     它同时是 WCAG 1.4.1「不只靠颜色」的文字侧：这两态的状态点
                     与 paused 同色（都是琥珀），区分靠状态小字与这个数字。 -->
                <span class="task-gap-chip" v-if="gapCount > 0"
                      :title="t('js.gaps.chip_title', { n: gapCount })">{{ t('js.gaps.chip', { n: gapCount }) }}</span>
                <!-- 行尾（耗时 + 动作组）包成一个元素：窄面板下行1 会换行
                     （.task-line1 的 flex-wrap），不包起来时只有动作组被甩到
                     下一行、左对齐挂在状态点底下，看着像另一条任务的按钮。
                     贴右的 margin-left:auto 也从耗时挪到了这里 —— 两个兄弟
                     各挂一个 auto 会平分剩余空间，把耗时推到行中间。 -->
                <div class="task-line1__tail">
                    <span class="task-time progress-detail">{{ timeText }}</span>
                    <div class="btn-group btn-group-sm">
                        <button v-if="hasTaskActions && isLive && supportsStart && task.status === 'pending'"
                                class="btn btn-icon btn-success" @click="act('startTask', $event)"
                                :title="t('js.history.action.start')" :aria-label="t('js.history.action.start')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                        <button v-if="hasTaskActions && isLive && supportsPauseResume && task.status === 'running'"
                                class="btn btn-icon btn-warning" @click="act('pauseTask', $event)"
                                :title="t('js.history.action.pause')" :aria-label="t('js.history.action.pause')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <rect x="6" y="4" width="4" height="16"></rect>
                                <rect x="14" y="4" width="4" height="16"></rect>
                            </svg>
                        </button>
                        <button v-if="hasTaskActions && isLive && supportsPauseResume && task.status === 'paused'"
                                class="btn btn-icon btn-success" @click="act('resumeTask', $event)"
                                :title="t('js.history.action.resume')" :aria-label="t('js.history.action.resume')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                        <!-- 预览：带洞的成品照样能预览 —— 它的瓦片目录是真的出了的
                             （accept_gaps 会把严格模式当初拒绝的拼接/复制补跑完）。
                             判据走 store 那份「产出可用」集合，不在模板里写
                             「status === 'completed'」这种字面量：写死过一次，实测一条
                             completed_with_gaps 的行上「导出 MBTiles」在、「在地图上
                             预览」不在 —— 同一份产物，两颗按钮给出互相矛盾的答案。 -->
                        <button v-if="canPreview && isSuccessful"
                                class="btn btn-icon btn-sm btn-success" @click="preview"
                                :title="t('js.history.action.preview')" :aria-label="t('js.history.action.preview')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                                <circle cx="12" cy="12" r="3"></circle>
                            </svg>
                        </button>
                        <!-- 「用它切地形」：把已完成的高程下载任务转成地形切片任务
                             （新任务进时间流）。打开的是 map.js 的新建任务面板并预选
                             「本地地形切片 + 这个任务」，所以与 canPreview 同一条把关
                             —— 独立页 /history 不加载 map.js，不渲染。
                             同样走 isSuccessful 而不是状态字面量：DEM 管线今天到不了
                             completed_with_gaps（全仓只有 task_manager.py 写那个状态），
                             但「状态字面量散在模板里」正是上一颗按钮出问题的机制本身，
                             留一个在这里就是留一颗同型的雷。 -->
                        <button v-if="canProcessDem && task.task_type === 'dem' && isSuccessful"
                                class="btn btn-icon btn-sm btn-primary" @click="processDem"
                                :title="t('js.history.action.process')" :aria-label="t('js.history.action.process')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path>
                            </svg>
                        </button>
                        <!-- 导出 MBTiles。§5.3：MBTiles 是**通用产物容器**，不是
                             第四种 output_format —— 同一个任务可以同时持有 XYZ 目录、
                             逐层 GeoTIFF 和一个 MBTiles，所以它是完成后的一个动作。
                             带缺口的成品同样可导出（后端 is_successful 含
                             completed_with_gaps）：排除它就等于让「接受缺口」这个
                             决定毫无意义。 -->
                        <button v-if="canExport && isExportable"
                                class="btn btn-icon btn-sm btn-secondary tf-btn" @click="exportOutput"
                                :title="t('js.gaps.action.export')" :aria-label="t('js.gaps.action.export')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path>
                                <polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline>
                                <line x1="12" y1="22.08" x2="12" y2="12"></line>
                            </svg>
                        </button>
                        <button class="btn btn-icon btn-sm btn-danger" @click="remove($event)"
                                :title="t('js.history.action.delete')" :aria-label="t('js.history.action.delete')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                        </button>
                    </div>
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
            <!-- pending_decision 刻意不渲染「已完成 · N/M」摘要：它**没有**完成。
                 那一行的第一个词是状态文案，写着「已完成」就是在说谎；缺口决策行
                 会说出真话（缺了多少、哪一档、下一步点什么）。 -->
            <template v-else-if="!isPendingDecision">
                <div class="task-line2">{{ summaryText }}{{ bboxText }}</div>
                <div class="task-line2 task-tiling-line" v-if="task.tiling_text">{{ task.tiling_text }}</div>
            </template>
            <!-- 缺口决策行（§13-3）。两态都渲染：pending_decision 给出两条出路，
                 completed_with_gaps 给出「补漏」（缺口不是终点，上游恢复后还能补）。
                 这一行**不是**进度行，所以没有进度条 —— 一根静止的条只会让人以为
                 「还在跑，再等等」。分档明细来自 GET /api/tasks/<id>/gaps，由
                 task_center.js 的 ensureGapSummary 拉一次挂到任务对象上；
                 task_gap_decision 推送里已经带全部数字，那条路不再多打一次请求。 -->
            <div class="task-gap-line" v-if="showGapLine">
                <!-- 三态：明细已到 / 还在读 / 读失败。第三态**必须**存在 ——
                     GET /gaps 超时一次，这一行从前就永久停在「正在读取缺块明细…」：
                     ensureGapSummary 不重试，而重新拉取的三个触发点（mounted 与
                     task.status / task.gap_tiles 两条 watch）在「同一个 keyed 行
                     原地不动、状态与缺块数都没变」时一个都不响。把失败摆出来、
                     把重试交给用户按一下，是不引入无条件轮询的唯一出路。 -->
                <span class="task-gap-breakdown" :title="gapLineText">{{ gapLineText }}</span>
                <!-- 强调**跟着数据走**，不是固定的。
                     全部缺块都是「上游无数据」时（explained），补漏一张也补不回来
                     （no_data 不在后端 RETRYABLE_OUTCOMES 里），该点的是「接受并
                     导出」—— 那时它是填充按钮、补漏退成描边。反之补漏有机会补回
                     一部分，它才是填充的那一颗。
                     把不可撤销的那一颗永久做成最醒目的按钮是错的：用户会顺着视觉
                     重量点下去，而「接受缺口」之后产物与历史永久带缺块标记。 -->
                <div class="task-gap-actions">
                    <button v-if="gapLoadError" type="button" class="btn btn-sm btn-outline-secondary"
                            @click="retryGaps" :title="t('js.gaps.action.retry_title')">{{ t('js.gaps.action.retry') }}</button>
                    <button v-if="canRefill" type="button" class="btn btn-sm"
                            :class="gapsExplained ? 'btn-outline-secondary' : 'btn-primary'"
                            @click="refill($event)" :title="t('js.gaps.action.refill_title')">{{ t('js.gaps.action.refill') }}</button>
                    <button v-if="canAcceptGaps" type="button" class="btn btn-sm"
                            :class="gapsExplained ? 'btn-warning' : 'btn-outline-secondary'"
                            @click="acceptGaps($event)" :title="t('js.gaps.action.accept_title')">{{ t('js.gaps.action.accept') }}</button>
                </div>
            </div>
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
            isPendingDecision() {
                return this.task.status === 'pending_decision';
            },
            // 缺口总数。socket 推送与 /api/tasks 都给 gap_tiles（Task.to_dict），
            // gap_summary.total 是 GET /gaps 的同一个数 —— 取两者中先有的那个。
            // 老行（迁移前建的任务）两者皆无，取 0：徽章不出现，与「没有缺口」
            // 同形，这正确 —— 迁移前的任务确实没有缺口记录。
            gapCount() {
                const t = this.task;
                if (t.gap_tiles != null) return t.gap_tiles;
                return (t.gap_summary && t.gap_summary.total) || 0;
            },
            // 缺口是瓦片级的概念。两条管线各有一份 `/gaps` 与「接受缺块」：
            // 地图的在 /api/tasks/<id>/…，插件的在 /api/plugins/tasks/<id>/…
            // （plugin_task_tiles 与 task_tiles 同构，status 都是 TileOutcome 值）。
            // 探测的是 acceptTaskGaps 而不是 refillTask：那才是两条管线都有的
            // 那个动作，而这个 typeof 只是在问「task_center.js 加载了吗」
            // （/config 上它整个不加载）。
            canDecideGaps() {
                return (this.task.task_type === 'map'
                        || this.task.task_type === 'plugin')
                    && typeof acceptTaskGaps === 'function';
            },
            canRefill() {
                // 插件管线**没有**补漏：宿主不知道插件的某一块洞该怎么重跑
                // （§13-4 产出与重试语义归插件），PluginTaskManager 只有
                // accept_gaps，也没有 /refill 端点。渲染出来就是一颗点下去
                // 必然 404 的按钮。
                return this.canDecideGaps
                    && this.task.task_type !== 'plugin'
                    && store.REFILLABLE_STATUSES.includes(this.task.status);
            },
            canAcceptGaps() {
                return this.canDecideGaps && this.isPendingDecision;
            },
            // 决策行：待决必显（它是这一行存在的理由），已接受的带洞成品在有
            // 缺口时也显示 —— 缺口不是终点，上游恢复后还能补。
            showGapLine() {
                return this.canDecideGaps
                    && store.GAP_STATUSES.includes(this.task.status)
                    && (this.isPendingDecision || this.gapCount > 0);
            },
            // 「补漏补不回任何东西」—— 决定两颗按钮谁是填充色。摘要还没拉回来时
            // 为 false，也就是先按「补漏有机会」呈现：那是可撤销的一侧，摘要到
            // 位后会自己翻过来。反过来默认（先把不可撤销的那颗做成填充按钮）会
            // 在最坏时机误导用户。
            gapsExplained() {
                return !!(this.task.gap_summary && this.task.gap_summary.explained);
            },
            // 空串**只**表示「摘要还没拉回来」—— 模板用它切到「正在读取…」。
            // 摘要到位但四档全为 0 是另一回事（历史行的 gap_tiles 与 task_tiles
            // 里的记录不一致时会出现），那时必须说「没有缺块记录」：继续显示
            // 「正在读取…」等于让那一行永远转圈，而它其实早就读完了。
            gapBreakdown() {
                const summary = this.task.gap_summary;
                if (!summary || typeof gapBreakdownText !== 'function') return '';
                return gapBreakdownText(summary.by_outcome) || this.t('js.gaps.none');
            },
            // 明细读取失败时服务端/网络给的原文（''=没失败）。由
            // task_center.js 的 ensureGapSummary 写进 store —— 只 console.error
            // 一句的那一版等于把失败藏起来，行上看不出与「还在读」有任何区别。
            gapLoadError() {
                return this.task.gap_summary_error || '';
            },
            // 这一行到底该说什么。顺序即优先级：拿到明细就说明细；没拿到但
            // 记着失败就说失败（旁边同时出「重试」）；两者都没有才是「正在读取」。
            gapLineText() {
                if (this.gapBreakdown) return this.gapBreakdown;
                if (this.gapLoadError) {
                    return this.t('js.gaps.load_failed', { error: this.gapLoadError });
                }
                return this.t('js.gaps.loading');
            },
            // 「产出可用（可能带洞）」：全站唯一一份在 task_store。预览、
            // 「处理」与导出三颗按钮共用它 —— 三处各写一遍 === 'completed'
            // 正是本次修掉的缺陷，加一个成功态就要满模板 grep。
            isSuccessful() {
                return store.SUCCESSFUL_STATUSES.includes(this.task.status);
            },
            // 导出按钮出不出现的两个前提：管线值得给按钮 + 产出可用（含带洞成品）。
            //
            // ⚠️ 这里是**粗筛**，不是最终答案。「这个任务导得出哪些格式」的真值
            // 在后端（mbtiles 看管线，插件格式要拿产物登记行对照每个导出器的
            // accepts()），行上问不起 —— 一行一个 GET 就是列表一屏几十发请求。
            // 所以真实答案由点击时的 GET /api/export/<pipeline>/<id>/formats 给出：
            // 一种直接导、多种弹选择、零种弹一句提示。
            canExport() {
                return typeof exportTask === 'function';
            },
            isExportable() {
                return EXPORTABLE_PIPELINES.includes(this.task.task_type)
                    && this.isSuccessful;
            },
            // 独立页 /history 不加载 tasks.js：那里只剩详情（点任务名）和删除。
            // 预览也没有 —— 它由 canPreview 单独把关，而 previewTask 来自 map.js，
            // /history 同样不加载。
            hasTaskActions() {
                return typeof startTask === 'function';
            },
            // 启动与暂停/恢复是**两个**开关，不是一个。本地地形与插件任务都没有
            // 暂停/恢复语义（前者后端状态机不支持，后者 PluginTaskManager 根本
            // 没有这两个方法），但插件任务**有**启动
            // （POST /api/plugins/tasks/<id>/start）—— 合成一个开关就会把它的
            // 启动按钮一起摘掉，pending 的插件任务从此没有任何入口能跑起来。
            supportsStart() {
                return this.task.task_type !== 'local_terrain';
            },
            supportsPauseResume() {
                return this.task.task_type !== 'local_terrain'
                    && this.task.task_type !== 'plugin';
            },
            // 预览只在首页覆盖面板里有意义（主视图在旁边），独立页没有主视图。
            // 插件任务不预览：map.js 的 previewTask 只认四条核心管线的瓦片/
            // 地形地址，插件的产出格式归插件自己（§13-4），宿主不知道它长什么
            // 样。不挡的话那是一颗点下去静默无反应的按钮 —— previewTask 的
            // task_type 分支链没有 else。
            canPreview() {
                return this.task.task_type !== 'plugin'
                    && typeof previewTask === 'function';
            },
            // 「用它切地形」打开的是 map.js 的新建任务面板，独立页 /history 不加载 map.js。
            canProcessDem() {
                return typeof openProcessForDemTask === 'function';
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
                // 插件任务归「瓦片」一侧：它的缺块记在 plugin_task_tiles，
                // 与 task_tiles 同构。normalizeTask 已经写好 items_label，
                // 这条回落兜的是 /api/history_all 直接进 store 的历史行
                // （replaceAll 不过 normalizeTask，那些行没有这个字段）。
                return t.items_label
                    || ((t.task_type === 'map' || t.task_type === 'contour'
                         || t.task_type === 'plugin')
                        ? this.t('js.history.unit.tile') : this.t('js.history.unit.file'));
            },
            // 切片百分比优先于计数：本地地形任务在切片期间仍是 running，而它的
            // 计数（上传的文件数）早就写满了 —— 不让位就是整段显示 100%。
            // 判 != null 而不是真值：0% 是合法进度（切片刚开始），`||` 会把它
            // 当成「没有值」再退回计数，于是那一发又跳回 100%。
            progress() {
                if (this.task.tiling_progress != null) return this.task.tiling_progress;
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
                // 读数档（ui.js 的 formatCoord，5 位）。这里原来是 toFixed(2)
                // —— ≈1.1 km，同一个选区在任务行和状态栏读出两个不同的框，
                // 用户拿它对不上自己刚框的范围。
                const f = window.formatCoord;
                return ' · ' + this.t('js.history.row.bbox', {
                    north: f(t.north), south: f(t.south),
                    east: f(t.east), west: f(t.west),
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
            // 切片阶段的「预计剩余」。行自己的计数在这一段恒为满，按它外推出来的
            // 剩余永远是 0（于是整段不显示），所以另算一份：拿**这一段自己的起点**
            // 与起点处的百分比（tasks.js updateTerrainJobProgress 写的锚），外推到
            // 100%。三个锚字段缺一不算 —— 只在逐瓦片那一段有，物化阶段刻意没有。
            tilingEstimated() {
                const task = this.task;
                if (task.tiling_phase !== 'tiles') return '';
                if (task.tiling_progress == null || task.tiling_started_at == null
                    || task.tiling_anchor_pct == null) return '';
                if (typeof calculateTimeInfo !== 'function') return '';
                void store.state.tick;      // 每秒重算，勿删
                const advanced = task.tiling_progress - task.tiling_anchor_pct;
                if (advanced <= 0) return '';      // 还没前进，外推是除零
                return calculateTimeInfo({
                    started_at: new Date(task.tiling_started_at).toISOString(),
                    status: 'running',
                    downloaded_items: advanced,
                    total_items: 100 - task.tiling_anchor_pct,
                }).estimated || '';
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
                // 已运行始终是整个任务的；预计剩余在切片期间换成切片那一段的。
                const estimated = this.tilingEstimated || info.estimated;
                return [
                    info.elapsed ? this.t('js.history.row.elapsed', { time: info.elapsed }) : '',
                    estimated ? this.t('js.history.row.estimated', { time: estimated }) : '',
                ].filter(Boolean).join(' · ') || '—';
            },
            // 下载速度。只在**下载阶段**出现，三种情况各自不同：
            //   - stageText 有值 = 已进入拼接 / 复制 / 切片，没有网络下载，隐藏；
            //   - speed_at 为空 = 后端这一发没带速度（等高线渲染阶段），隐藏；
            //   - speed_at 过期 = 推送停了但任务还在 running（网断了却没判失败），
            //     显示 0 B/s。不这么做界面会永远冻在最后那个 2.3 MB/s，看着像还在跑。
            speedText() {
                // 补漏（retrying）与首次下载一样在拉瓦片、一样有速度可报。
                // 判据走 store 的「此刻真在占资源」清单，不在这里写状态字面量 ——
                // 写死 `=== 'running'` 正是补漏上线后这一行漏掉速度的原因。
                if (!store.isRunning(this.task)) return '';
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
            // 动作函数是全局的（task_center.js / history.js 定义，被 map.js/
            // panels.js 也引用），这里按名字转发而不是直接绑定：/config 上它们
            // 不存在，v-if 已经挡住渲染，这层 typeof 是第二道防线。
            //
            // in-flight 上锁在**这一层**：触发元素只有这里拿得到
            // （$event.currentTarget 而不是 target —— 点在按钮里的 <svg> 上时
            // target 是那个 svg）。所以转发下去时**不再**把按钮带上：那些动作
            // 函数收到 trigger 时自己也会 guard，同一颗按钮套两层守卫的话里层
            // 会看见「已在飞」而直接返回，动作一次都发不出去。
            act(fnName, event) {
                const fn = window[fnName];
                if (typeof fn !== 'function') return undefined;
                const task = this.task;
                return guard(event && event.currentTarget, function () {
                    return fn(task.id, task.task_type);
                });
            },
            viewDetails() {
                if (typeof viewTaskDetails === 'function') viewTaskDetails(this.task.id, this.task.task_type);
            },
            preview() {
                if (typeof previewHistoryTask === 'function') previewHistoryTask(this.task.id, this.task.task_type);
            },
            processDem() {
                if (typeof openProcessForDemTask === 'function') openProcessForDemTask(this.task.id);
            },
            // 删除 / 补漏 / 接受缺块与上面三个动作同形（同样的两个实参、同样的
            // typeof 探测、同样要上锁），所以走同一个 act：三处各写一遍
            // `if (typeof X === 'function') X(...)` 就是三份会各自漂移的守卫。
            remove(event) {
                return this.act('deleteTask', event);
            },
            refill(event) {
                return this.act('refillTask', event);
            },
            acceptGaps(event) {
                return this.act('acceptTaskGaps', event);
            },
            // 传按钮进去让它自己上锁：打包几万张瓦片要几十秒，不锁就会被连点，
            // 后端每一发都真的重打一遍同一个文件。$event.currentTarget 而不是
            // target —— 点在按钮里的 <svg> 上时 target 是那个 svg。
            exportOutput(event) {
                if (typeof exportTask !== 'function') return;
                exportTask(this.task.id, this.task.task_type,
                           event && event.currentTarget);
            },
            // 待决任务的分档明细要多一次 GET /gaps（socket 推送带 by_outcome，
            // 但页面刷新后重新拉列表时那份数据不在响应里）。ensureGapSummary
            // 自己按 key 去重并且幂等，所以这里可以无脑调。
            syncGaps() {
                // 判据是 showGapLine 而不是 isPendingDecision。曾经只拉待决那一态，
                // 于是整页刷新之后的 completed_with_gaps 行永久停在「正在读取缺块
                // 明细…」：那种行的 gap_tiles 来自 /api/history_all，而分档明细只在
                // task_gap_decision 推送或 GET /gaps 里，而刷新之后两者都不会发生。
                // 顺带把「补漏值不值得跑」的强调也修对了 —— gapsExplained 要读
                // gap_summary.explained，拉不到它就永远按「补漏有机会」呈现。
                if (!this.showGapLine) return;
                if (this.task.gap_summary || typeof ensureGapSummary !== 'function') return;
                ensureGapSummary(this.rowKey, this.task.id, this.task.task_type);
            },
            // 「重试」按钮。不清标记、不加计数：ensureGapSummary 自己在请求
            // 起手就把 gap_summary_error 清掉（行立刻回到「正在读取…」，用户
            // 看得见按下去有反应），并且带在飞去重，所以连点是安全的。
            retryGaps() {
                if (typeof ensureGapSummary !== 'function') return;
                ensureGapSummary(this.rowKey, this.task.id, this.task.task_type);
            },
        },
        // 三个时机都要拉：mounted 兜首屏与翻页（行是新建的），两条 watch 兜「同一个
        // keyed 行原地变了」（不会重新 mount）—— 状态翻成待决是一条，缺块数从 0
        // 变成非 0 是另一条（终态行的 gap_tiles 可以由推送单独补上，那一发不带
        // status，只 watch status 会漏掉它，行上就永久停在「正在读取缺块明细…」）。
        mounted() {
            this.syncGaps();
        },
        watch: {
            'task.status': function () {
                this.syncGaps();
            },
            'task.gap_tiles': function () {
                this.syncGaps();
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
        methods: {
            // 重发列表那一次 GET。页码取 currentPage（history.js 的全局，只在
            // 拉取**成功**后才写），失败时它还是上一次看得见的那一页 —— 拿它重
            // 试，用户不会因为一次网络抖动被弹回第 1 页。成功后 store.replaceAll
            // 自己会把 loadError 清掉，这条分支连带这颗按钮一起消失。
            reloadList(event) {
                if (typeof loadHistory !== 'function') return undefined;
                const page = typeof currentPage === 'number' && currentPage > 0
                    ? currentPage : 1;
                return guard(event && event.currentTarget, function () {
                    return loadHistory(page);
                });
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
