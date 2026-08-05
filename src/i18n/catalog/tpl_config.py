"""templates/_config_content.html + templates/config.html（配置页） 的界面文案。

key 命名：`tpl.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    'tpl.config.page_title': {
        'zh': '配置',
        'en': 'Settings',
    },
    'tpl.config.back_home': {
        'zh': '返回首页',
        'en': 'Back to home',
    },

    # 外观
    'tpl.config.appearance.title': {
        'zh': '外观',
        'en': 'Appearance',
    },
    'tpl.config.appearance.theme': {
        'zh': '主题',
        'en': 'Theme',
    },
    'tpl.config.appearance.theme_dark': {
        'zh': '暗黑',
        'en': 'Dark',
    },
    'tpl.config.appearance.theme_light': {
        'zh': '明亮',
        'en': 'Light',
    },
    'tpl.config.appearance.theme_system': {
        'zh': '跟随系统',
        'en': 'System',
    },
    'tpl.config.appearance.theme_hint': {
        'zh': '「跟随系统」按系统的明暗配色自动切换；选择立即生效，无需保存。',
        'en': '"System" follows the OS light/dark setting. The choice applies '
              'immediately, no need to save.',
    },

    # 基础设置
    'tpl.config.basic.title': {
        'zh': '基础设置',
        'en': 'Basic settings',
    },
    'tpl.config.basic.save_path': {
        'zh': '默认保存路径',
        'en': 'Default save path',
    },
    'tpl.config.basic.save_path_placeholder': {
        'zh': '绝对路径,可点「浏览」选择',
        'en': 'Absolute path, or click "Browse" to pick one',
    },
    'tpl.config.basic.browse': {
        'zh': '浏览',
        'en': 'Browse',
    },
    'tpl.config.basic.save_path_hint': {
        'zh': '必须是绝对路径且位于下载根目录之内。',
        'en': 'Must be an absolute path inside the download root directory.',
    },
    'tpl.config.basic.default_style': {
        'zh': '默认地图样式',
        'en': 'Default map style',
    },
    'tpl.config.basic.style_standard': {
        'zh': '标准',
        'en': 'Standard',
    },
    'tpl.config.basic.style_satellite': {
        'zh': '卫星',
        'en': 'Satellite',
    },
    'tpl.config.basic.style_satellite_labels': {
        'zh': '卫星+标注',
        'en': 'Satellite + labels',
    },
    'tpl.config.basic.style_roads': {
        'zh': '道路',
        'en': 'Roads',
    },
    'tpl.config.basic.style_terrain': {
        'zh': '地形',
        'en': 'Terrain',
    },
    'tpl.config.basic.zoom_min': {
        'zh': '默认最小缩放',
        'en': 'Default min zoom level',
    },
    'tpl.config.basic.zoom_max': {
        'zh': '默认最大缩放',
        'en': 'Default max zoom level',
    },

    # 下载设置
    'tpl.config.download.title': {
        'zh': '下载设置',
        'en': 'Download settings',
    },
    'tpl.config.download.concurrency': {
        'zh': '并发下载数',
        'en': 'Concurrent downloads',
    },
    'tpl.config.download.concurrency_recommend': {
        'zh': '测速推荐',
        'en': 'Benchmark',
    },
    'tpl.config.download.concurrency_hint': {
        'zh': '实测网络吞吐后推荐（约 30 秒），只填入数值，保存后生效。',
        'en': 'Measures real network throughput (about 30 s) and suggests a '
              'value. It only fills the field, save to apply.',
    },
    'tpl.config.download.request_timeout': {
        'zh': '请求超时(秒)',
        'en': 'Request timeout (s)',
    },
    'tpl.config.download.max_retries': {
        'zh': '最大重试次数',
        'en': 'Max retries',
    },
    'tpl.config.download.proxy': {
        'zh': '代理服务器 (可选)',
        'en': 'Proxy server (optional)',
    },
    'tpl.config.download.tile_servers': {
        'zh': '瓦片服务器列表',
        'en': 'Tile servers',
    },
    'tpl.config.download.tile_verify': {
        'zh': '验证',
        'en': 'Verify',
    },
    'tpl.config.download.tile_remove': {
        'zh': '删除该服务器',
        'en': 'Remove this server',
    },
    'tpl.config.download.tile_add': {
        'zh': '+ 添加服务器',
        'en': '+ Add server',
    },
    # 含 {z}/{x}/{y}/{style} 字面花括号：t() 不带 params 时不走 str.format，
    # 这里刻意不传参，花括号原样输出。
    'tpl.config.download.tile_servers_hint': {
        'zh': '每行一个：Google 别名（mts0–mts3）、主机（如 mts0.google.cn）或完整 XYZ 模板（含 {z}/{x}/{y}，可选 {style}）。下载按列表轮换；第一个条目同时用作地图底图。',
        'en': 'One per line: a Google alias (mts0–mts3), a host (e.g. '
              'mts0.google.cn) or a full XYZ template (with {z}/{x}/{y}, '
              'optionally {style}). Downloads rotate through the list; the '
              'first entry is also used as the map basemap.',
    },

    # 缓存设置
    'tpl.config.cache.title': {
        'zh': '缓存设置',
        'en': 'Cache settings',
    },
    'tpl.config.cache.enabled': {
        'zh': '启用瓦片缓存',
        'en': 'Enable tile cache',
    },
    'tpl.config.cache.manage': {
        'zh': '缓存管理',
        'en': 'Cache management',
    },
    'tpl.config.cache.manage_hint': {
        'zh': '缓存不会自动清理。按分类查看占用，可手动清理（需二次确认，删除后不可恢复）。',
        'en': 'The cache is never cleared automatically. Review usage by '
              'category and clear it manually (asks for confirmation, deleted '
              'data cannot be recovered).',
    },
    'tpl.config.cache.loading': {
        'zh': '加载中…',
        'en': 'Loading…',
    },
    'tpl.config.cache.total': {
        'zh': '总计',
        'en': 'Total',
    },
    # {files} 是那个由 JS 实时更新的 <span>，整条一起翻，避免中英括号/语序拼不回来。
    'tpl.config.cache.total_files': {
        'zh': '（{files} 个文件）',
        'en': ' ({files} files)',
    },
    'tpl.config.cache.refresh': {
        'zh': '刷新',
        'en': 'Refresh',
    },
    'tpl.config.cache.clear_all': {
        'zh': '全部清理',
        'en': 'Clear all',
    },

    # GDAL 设置
    'tpl.config.gdal.title': {
        'zh': 'GDAL 设置',
        'en': 'GDAL settings',
    },
    'tpl.config.gdal.compression': {
        'zh': '压缩方式',
        'en': 'Compression',
    },
    'tpl.config.gdal.compression_none': {
        'zh': '无压缩',
        'en': 'None',
    },
    'tpl.config.gdal.resampling': {
        'zh': '重采样算法',
        'en': 'Resampling',
    },
    'tpl.config.gdal.resampling_nearest': {
        'zh': '最近邻',
        'en': 'Nearest neighbour',
    },
    'tpl.config.gdal.resampling_bilinear': {
        'zh': '双线性',
        'en': 'Bilinear',
    },
    'tpl.config.gdal.resampling_cubic': {
        'zh': '三次卷积',
        'en': 'Cubic',
    },

    # 其他设置
    'tpl.config.misc.title': {
        'zh': '其他设置',
        'en': 'Other settings',
    },
    'tpl.config.misc.map_center_lat': {
        'zh': '地图中心纬度',
        'en': 'Map center latitude',
    },
    'tpl.config.misc.map_center_lng': {
        'zh': '地图中心经度',
        'en': 'Map center longitude',
    },
    'tpl.config.misc.map_initial_zoom': {
        'zh': '地图初始缩放',
        'en': 'Map initial zoom level',
    },

    # Earthdata 设置
    'tpl.config.earthdata.title': {
        'zh': 'Earthdata 设置',
        'en': 'Earthdata settings',
    },
    'tpl.config.earthdata.username': {
        'zh': 'Earthdata 用户名',
        'en': 'Earthdata username',
    },
    'tpl.config.earthdata.password': {
        'zh': 'Earthdata 密码',
        'en': 'Earthdata password',
    },

    # 底部按钮
    'tpl.config.actions.reset': {
        'zh': '重置为默认值',
        'en': 'Reset to defaults',
    },
    'tpl.config.actions.save': {
        'zh': '保存配置',
        'en': 'Save settings',
    },
}
