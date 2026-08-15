/**
 * 任务状态的显示映射（颜色 / 文案 / 地图描边色）——**全站唯一一份**。
 *
 * ## 为什么要有这个文件
 *
 * 改造前 getStatusColor / getStatusText 在 tasks.js 和 history.js 各有一份
 * 顶层声明。旧注释给的理由是「两个页面不会同时加载，收敛属于第三档」——
 * 那个前提是**错的**：index.html 的 extra_js 把 map.js / tasks.js /
 * history.js / config.js / panels.js 全部一起加载，五个文件共享同一个全局
 * 作用域，后加载的 history.js 会静默遮蔽 tasks.js 的同名函数。
 *
 * 后果不是「将来可能漂移」，而是当时就已经存在的两个缺陷：
 *   1. 两份 getStatusText 查的是**不同的** i18n key 前缀
 *      （js.tasks.status.* vs js.history.status.*）。两套文案逐字相同纯属
 *      巧合，改任何一份在首页都不生效 —— 首页跑的永远是 history.js 那份。
 *   2. 独立页 /history 与首页跑的是不同实现（独立页不加载 tasks.js）。
 *      同一个状态在两个页面由两段代码决定显示，这正是「守住了集合、
 *      没守住配对」那类 bug 的温床。
 *
 * 所以文案 key 也一并收口到 js.tasks.status.*，js.history.status.* 那 6 个
 * 键随之删除 —— 一个状态一处文案。
 *
 * ## 加载位置
 * base.html 的公共脚本区，必须排在 tasks.js / history.js / task_list.js
 * 之前（它们在函数体里调这三个函数；task_list.js 还要挂到 Vue 的
 * globalProperties 上）。i18n.js 必须更早：本文件的 getStatusText 调 t()。
 */

// 与 src/models/task.py 的 TaskStatus 八态对齐。
//
// running 用 'info' 而不是 'primary'：徽章侧 .status-badge.running /
// .badge.bg-primary / .badge.bg-info 是同一条声明块，渲染完全一致；
// 而进度条侧 .progress-bar.bg-info 已经存在，不必再写 .bg-primary 覆盖。
//
// 2026-08 §13-3 新增三态，**刻意复用已有的四档语义色**，一个新颜色名都不加：
//   retrying            -> info    与 running 同族：它就是在下载，只是第二遍。
//   pending_decision    -> warning 与 paused 同族：都是「停下来等你」。
//   completed_with_gaps -> warning **不是** success：产物带洞是一个必须被看见
//                          的事实，画成绿色等于把它伪装成干净的成功。
// 为什么不给它们各自一个新颜色名：每个新颜色名都要在 style.css 里配一条
// `.progress-bar.bg-X { ... !important }`（压 Bootstrap 自带 !important 的
// 工具类，理由见 test_important_count_under_control），而 !important 的
// 总量上界是 37、当前实测 34 —— 三个新颜色名会把余量一次吃光。
// 颜色不是唯一的区分手段：行1 有 .task-status-text 文字，缺口两态另有常驻的
// .task-gap-chip 数字徽章（WCAG 1.4.1 的「不只靠颜色」由它们承担）。
function getStatusColor(status) {
    const colors = {
        'pending': 'secondary',
        'running': 'info',
        'retrying': 'info',
        'paused': 'warning',
        'pending_decision': 'warning',
        'completed': 'success',
        'completed_with_gaps': 'warning',
        'failed': 'danger'
    };
    // 查表走 hasOwnProperty，与 history.js 的档位表 / 删除确认表同一条约定：
    // 对象字面量继承 Object.prototype，status === 'constructor' / '__proto__' /
    // 'toString' 时裸下标会取到原型上的成员。那些值都是真值，`|| 'secondary'`
    // 根本兜不到，class 变成 `bg-function Object() { [native code] }`，
    // 徽章静默退化成无色 —— 界面上看不出这是个坏状态值。
    return (Object.prototype.hasOwnProperty.call(colors, status) && colors[status])
        || 'secondary';
}

function getStatusText(status) {
    const texts = {
        'pending': t('js.tasks.status.pending'),
        'running': t('js.tasks.status.running'),
        'retrying': t('js.tasks.status.retrying'),
        'paused': t('js.tasks.status.paused'),
        'pending_decision': t('js.tasks.status.pending_decision'),
        'completed': t('js.tasks.status.completed'),
        'completed_with_gaps': t('js.tasks.status.completed_with_gaps'),
        'failed': t('js.tasks.status.failed')
    };
    // 未知状态不把英文字面量原样渲染进中文界面（A7 修过的中英混杂问题）。
    // hasOwnProperty 同 getStatusColor：裸下标下 status === 'constructor'
    // 会取到构造函数，兜底分支永远走不到，徽章里是一坨函数源码。
    return (Object.prototype.hasOwnProperty.call(texts, status) && texts[status])
        || t('js.tasks.status.unknown');
}

// 历史地图上矩形的描边色。
//
// 改前是内联三元阶梯，只认 completed / failed，其余三态（pending / running /
// paused）全折叠成同一个蓝色。而且三个色号
// #10b981 / #ef4444 / #60a5fa 是**硬编码且离调色板**的：#10b981 是
// emerald-500，本项目的 --color-success 是 emerald-400 #34d399，改调色板时
// 这里会静默漂移。
//
// 现在读 CSS 自定义属性，与徽章/进度条/卡片边条走同一套语义令牌：
//   pending -> --color-text-secondary（与 .badge.bg-secondary 同色）
// 其余四态各自的语义令牌见下表。
// Cesium 要的是真实色值字符串，不认 var()，所以必须在这里求值。
//
// 惰性缓存：getComputedStyle 每次调用都强制样式计算，renderHistoryMap 逐
// 任务调用时成本放大；首次调用把令牌求值后查表。
let _statusStrokeCache = null;

const _STATUS_STROKE_TOKENS = {
    'pending': '--color-text-secondary',
    'running': '--color-info',
    'retrying': '--color-info',
    'paused': '--color-warning',
    'pending_decision': '--color-warning',
    'completed': '--color-success',
    'completed_with_gaps': '--color-warning',
    'failed': '--color-danger'
};

function getStatusStroke(status) {
    // 同 getStatusColor 的理由：裸下标下 status === 'constructor' 会取到构造
    // 函数，`||` 兜不到，name 不是令牌名，下面查缓存得到 undefined，
    // Cesium 拿到 undefined 描边色 —— 矩形边框静默消失。
    const name = (Object.prototype.hasOwnProperty.call(_STATUS_STROKE_TOKENS, status)
        && _STATUS_STROKE_TOKENS[status]) || '--color-text-secondary';
    if (!_statusStrokeCache) {
        const style = getComputedStyle(document.documentElement);
        _statusStrokeCache = {};
        Object.keys(_STATUS_STROKE_TOKENS).forEach(function (key) {
            const token = _STATUS_STROKE_TOKENS[key];
            _statusStrokeCache[token] = style.getPropertyValue(token).trim();
        });
    }
    return _statusStrokeCache[name];
}

// U5：主题切换后缓存必须失效并重画 —— 缓存的前提「调色板运行期不变」在
// 主题开关落地后已经不成立（亮色块覆盖了这 6 个令牌全部）。getStatusStroke
// 只在 renderHistoryMap 里被调用，切主题本身不会触发重渲染，所以要显式重画。
// renderHistoryMap 是 history.js 的全局，没有地图的页面（/config）加载本文件
// 时它不存在，typeof 守卫兜底。它自己从 TaskStore 取数据，所以这里不传参 ——
// 改前传的是 history.js 的 allTasks（loadHistory 的响应快照），主题切换时会
// 拿一份可能已经过期的列表重画。
document.addEventListener('terraforge:themechange', function () {
    _statusStrokeCache = null;
    if (typeof renderHistoryMap !== 'function') return;
    try { renderHistoryMap(); } catch (e) { /* 明确忽略：地图未就绪，下次重绘自会补上 */ }
});
