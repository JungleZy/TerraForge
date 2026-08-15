"""templates/base.html（外壳、底部状态栏、任务详情弹窗） 的界面文案。

key 命名：`tpl.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # 底部状态栏
    'tpl.base.conn.disconnected': {
        'zh': '未连接',
        'en': 'Disconnected',
    },

    # 任务详情弹窗 —— 外壳
    'tpl.base.detail.title': {
        'zh': '任务详情',
        'en': 'Task details',
    },
    'tpl.base.detail.close': {
        'zh': '关闭',
        'en': 'Close',
    },

    # 任务详情弹窗 —— 基本字段
    'tpl.base.detail.task_id': {
        'zh': '任务ID',
        'en': 'Task ID',
    },
    'tpl.base.detail.task_name': {
        'zh': '任务名称',
        'en': 'Task name',
    },
    'tpl.base.detail.status': {
        'zh': '状态',
        'en': 'Status',
    },
    'tpl.base.detail.map_style': {
        'zh': '地图样式',
        'en': 'Map style',
    },
    'tpl.base.detail.output_format': {
        'zh': '输出格式',
        'en': 'Output format',
    },
    'tpl.base.detail.zoom': {
        'zh': '缩放层级',
        'en': 'Zoom level',
    },
    # 英文不能写 'Total tiles'：这一格（`#detailTotal`）在五条管线上装的是**不同**
    # 的计数 —— 地图/等高线是 total_tiles，高程是 total_files（颗粒文件），本地地形
    # 是 total_files（上传的文件），插件是 total_items
    # （static/js/history.js:444,471,478,492,499）。中文「总数量」本来就是单位中立的，
    # 英文照抄 'Total' 会与另外两处撞名，所以用同样中立的 'Total items'。
    'tpl.base.detail.total': {
        'zh': '总数量',
        'en': 'Total items',
    },
    'tpl.base.detail.downloaded': {
        'zh': '已下载',
        'en': 'Downloaded',
    },
    'tpl.base.detail.failed': {
        'zh': '失败',
        'en': 'Failed',
    },
    'tpl.base.detail.progress': {
        'zh': '进度',
        'en': 'Progress',
    },

    # 任务详情弹窗 —— 区域范围
    'tpl.base.detail.bbox': {
        'zh': '区域范围',
        'en': 'Area extent',
    },
    'tpl.base.detail.north': {
        'zh': '北纬',
        'en': 'North',
    },
    'tpl.base.detail.south': {
        'zh': '南纬',
        'en': 'South',
    },
    'tpl.base.detail.east': {
        'zh': '东经',
        'en': 'East',
    },
    'tpl.base.detail.west': {
        'zh': '西经',
        'en': 'West',
    },

    # 任务详情弹窗 —— 输出与时间
    'tpl.base.detail.output_path': {
        'zh': '保存路径',
        'en': 'Save path',
    },
    'tpl.base.detail.created_at': {
        'zh': '创建时间',
        'en': 'Created',
    },
    'tpl.base.detail.started_at': {
        'zh': '开始时间',
        'en': 'Started',
    },
    'tpl.base.detail.completed_at': {
        'zh': '完成时间',
        'en': 'Completed',
    },

    # 任务详情弹窗 —— 错误
    'tpl.base.detail.error_label': {
        'zh': '错误信息:',
        'en': 'Error message:',
    },

    # 任务详情弹窗 —— 地形切片
    'tpl.base.detail.terrain_label': {
        'zh': '地形切片:',
        'en': 'Terrain tiling:',
    },

    # 任务详情弹窗 —— 缺口与任务日志（阶段 3）
    # 「缺口」不叫「失败」:no_data（该处本来就没有影像）与真失败在这里是同一栏
    # 里的两类,标签只能中性 —— 叫「失败」会让一个正常完成的近海任务看起来像出错。
    'tpl.base.detail.gaps_label': {
        'zh': '缺块',
        'en': 'Gaps',
    },
    # 「产物」而不是「输出文件」：一件产物可以是目录（XYZ 金字塔、地形目录），
    # 也可以是单文件（MBTiles、逐层 GeoTIFF），「文件」只说对了一半。
    'tpl.base.detail.artifacts_label': {
        'zh': '产物',
        'en': 'Artifacts',
    },
    'tpl.base.detail.log_label': {
        'zh': '任务日志',
        'en': 'Task log',
    },

    # 命令面板(Ctrl/Cmd+K)与快捷键速查 —— 外壳
    'tpl.base.cmdk.title': {
        'zh': '命令面板',
        'en': 'Command Palette',
    },
    'tpl.base.cmdk.placeholder': {
        'zh': '输入命令…',
        'en': 'Type a command…',
    },
    'tpl.base.cmdk.help_title': {
        'zh': '快捷键',
        'en': 'Keyboard Shortcuts',
    },
    'tpl.base.cmdk.help_close': {
        'zh': '关闭',
        'en': 'Close',
    },
}
