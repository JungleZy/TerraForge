"""templates/_config_content.html + templates/config.html（配置页） 的界面文案。

key 命名：`tpl.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # 说明图标（.hint）无障碍名的**前缀**。宏把它拼成
    # `aria-label="<前缀>: <正文>"` —— 气泡文本在 data-hint 上、由 ::after
    # 渲染,而 aria-label 按 accname 规范优先于 CSS 生成内容,所以正文只有
    # 拼进 aria-label 才进得了无障碍树。改前这里只有动作名,9 条说明对读屏
    # 用户完全不可见。
    'tpl.config.hint.aria': {
        'zh': '查看说明',
        'en': 'Show help',
    },
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
    'tpl.config.appearance.accent': {
        'zh': '强调色',
        'en': 'Accent',
    },
    'tpl.config.appearance.accent_hint': {
        'zh': '界面强调色预设，默认为品牌蓝;即时生效，偏好只保存在本机。',
        'en': 'Accent color preset. Defaults to brand sky blue; applies instantly and is stored locally only.',
    },
    'tpl.config.appearance.accent_sky': {
        'zh': '品牌蓝',
        'en': 'Sky',
    },
    'tpl.config.appearance.accent_teal': {
        'zh': '青',
        'en': 'Teal',
    },
    'tpl.config.appearance.accent_violet': {
        'zh': '紫',
        'en': 'Violet',
    },
    'tpl.config.appearance.accent_rose': {
        'zh': '玫红',
        'en': 'Rose',
    },
    'tpl.config.appearance.accent_orange': {
        'zh': '橙',
        'en': 'Orange',
    },
    'tpl.config.appearance.theme_hint': {
        'zh': '「跟随系统」按系统的明暗配色自动切换；选择立即生效，无需保存。',
        'en': '"System" follows the OS light/dark setting. The choice applies '
              'immediately, no need to save.',
    },

    # 基础设置
    'tpl.config.basic.title': {
        'zh': '基础配置',
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
    # 「默认地图样式」→「底图样式」（2026-08-15，设计稿 §2.4 的四个「假旋钮」
    # 之一）。原文案承诺它给下载表单的样式下拉设默认值 —— 它不做这件事：
    # #mapStyle 永远停在第一个 option，因为 default_style 不在
    # routes/main.MAP_CONFIG_KEYS 白名单里，根本进不了页面级 config。
    # 它真正喂的是 Cesium 底图与瓦片代理（services/basemap_source.resolve_basemap
    # 的 default_style 形参、routes/basemap_static、api.py 那两处），所以标签
    # 改成它真正控制的那个东西。
    'tpl.config.basic.default_style': {
        'zh': '底图样式',
        'en': 'Basemap style',
    },
    'tpl.config.basic.style_standard': {
        'zh': '路网',
        'en': 'Roadmap',
    },
    'tpl.config.basic.style_satellite': {
        'zh': '卫星影像',
        'en': 'Satellite imagery',
    },
    'tpl.config.basic.style_satellite_labels': {
        'zh': '卫星影像+标注',
        'en': 'Satellite imagery + labels',
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
        'zh': '下载配置',
        'en': 'Download settings',
    },
    'tpl.config.download.concurrency': {
        'zh': '并发下载数',
        'en': 'Concurrent downloads',
    },
    'tpl.config.download.concurrency_recommend': {
        'zh': '测速并填入',
        'en': 'Benchmark & fill in',
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
    'tpl.config.download.proxy_hint': {
        'zh': '留空即交给下方的自动检测；填了就以填的为准。',
        'en': 'Leave empty to let auto-detection handle it; a value here always '
              'wins.',
    },
    'tpl.config.download.proxy_auto': {
        'zh': '自动检测代理',
        'en': 'Auto-detect proxy',
    },
    'tpl.config.download.proxy_auto_hint': {
        'zh': '代理服务器留空时，自动查找可用代理：环境变量与系统代理设置、Windows 的 PAC 自动配置脚本、本机（WSL 下含 Windows 宿主）上 Clash/v2rayN 等常见代理端口。每个候选都会用一张真实瓦片实测，通过了才采用；都不通就直连。',
        'en': 'When the proxy field is empty, look for a working proxy: '
              'environment variables and OS proxy settings, the Windows PAC '
              'script, and the common Clash/v2rayN proxy ports on this machine '
              '(plus the Windows host when running under WSL). Every candidate '
              'is checked with a real tile request and only adopted if it '
              'works; if none do, it connects directly.',
    },
    'tpl.config.download.proxy_detect': {
        'zh': '立即检测',
        'en': 'Detect now',
    },
    # 代理状态图标（#proxyStatusIcon）：颜色表状态，完整说明由 config.js
    # 写进 data-hint。这里是读屏用的固定动作名。
    'tpl.config.download.proxy_status_aria': {
        'zh': '代理检测状态',
        'en': 'Proxy detection status',
    },
    'tpl.config.download.tile_servers': {
        'zh': '瓦片服务器列表',
        'en': 'Tile servers',
    },
    'tpl.config.download.basemap': {
        'zh': '地图底图',
        'en': 'Basemap',
    },
    # 这里刻意不传参，花括号原样输出。
    #
    # 底图瓦片自 basemap_static 蓝图落地起就由服务端取，和下载共用
    # proxy_autodetect.resolve_from_config —— 这段文案曾经写着相反的话，
    # 而「底图打不开」的用户正是照着它跳过了唯一能修好它的那一步。
    #
    # 只说「服务端转发」，不说「同源」：自 0.3 起瓦片默认由**另一个端口**
    # （5001，src/core/tile_server.py）出图，只有降级回主端口时才真是同源。
    # 这句话要传达的是「这一跳在服务端、所以吃代理」，同源与否与它无关。
    'tpl.config.download.basemap_hint': {
        'zh': '框选时看到的底图由服务端转发（/basemap/{z}/{x}/{y}），与下载走'
              '同一条出网路径，一样吃代理服务器配置与代理自动检测 —— 底图加载不出来'
              '（只剩一个蓝色球体）时，先去检查上面的代理。Esri 卫星影像与 Google '
              '卫星影像同为 WGS-84，框选位置对得上；不要填高德/腾讯的卫星地址，它们是 '
              'GCJ-02 偏移坐标，在国内会错位数百米。自定义需填完整 XYZ 模板'
              '（含 {z}/{x}/{y}）。',
        'en': 'The imagery you see while drawing a box is fetched by the '
              'backend (/basemap/{z}/{x}/{y}): it takes the same network path as '
              'downloads and obeys the proxy server setting and proxy '
              'auto-detection — if it will not load (you only get a blue globe), '
              'check the proxy above first. Esri satellite imagery shares WGS-84 '
              'with Google satellite imagery, so the box you draw matches what you '
              'download; do not point '
              'this at AMap or Tencent satellite tiles — they are GCJ-02 shifted and '
              'will be off by hundreds of metres in China. Custom requires a full '
              'XYZ template (with {z}/{x}/{y}).',
    },
    'tpl.config.download.basemap_esri': {
        'zh': 'Esri 卫星影像（推荐，国内直连可用）',
        'en': 'Esri satellite imagery (recommended)',
    },
    'tpl.config.download.basemap_google_sat': {
        'zh': 'Google 卫星影像（国内通常需要代理）',
        'en': 'Google satellite imagery',
    },
    'tpl.config.download.basemap_google_map': {
        'zh': 'Google 路网（国内通常需要代理）',
        'en': 'Google roadmap',
    },
    'tpl.config.download.basemap_follow': {
        'zh': '跟随下载源（用上面列表的第一条）',
        'en': 'Follow download source (first entry above)',
    },
    'tpl.config.download.basemap_custom': {
        'zh': '自定义 XYZ 模板',
        'en': 'Custom XYZ template',
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
        'zh': '每行一个：Google 别名（mts0–mts3）、主机（如 mts0.google.cn）或完整 XYZ 模板（含 {z}/{x}/{y}，可选 {style}）。下载按列表轮换。底图是**另一项**配置，见下面的「地图底图」。',
        'en': 'One per line: a Google alias (mts0–mts3), a host (e.g. '
              'mts0.google.cn) or a full XYZ template (with {z}/{x}/{y}, '
              'optionally {style}). Downloads rotate through the list. The '
              'basemap is a separate setting — see "Basemap" below.',
    },

    # 缓存设置
    'tpl.config.cache.title': {
        'zh': '缓存配置',
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
        'en': 'Total size',
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
        'zh': 'GDAL 配置',
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
        'zh': '其他配置',
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
        'zh': 'Earthdata 配置',
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

    # 地名搜索。出厂为空 = 功能关闭（不内置任何 provider 是产品决定，
    # 见 services/geocoding 的模块 docstring）。这三条是用户唯一能自己
    # 把它打开的地方。
    #
    # 示例给的是**真实可用**的地址而不是 example.org 占位串（用户要求）。
    # 这与「不内置数据源」不冲突：字段出厂仍为空，示例只是提示文本，要不要
    # 用由使用者复制粘贴那一下决定，也没有任何 token 进源码
    # （docs/notes/external-projects-takeaways.md §11 禁的是前两者）。
    # Photon 排在前面的理由是实测出来的：免注册免 key，且返回本地语言名称
    # （查「重庆」得到「重庆市」）。Nominatim 是本模块 _parse_nominatim 对着写的
    # 那一家，但它在部分网络环境下解析不到（实测本机 openstreetmap.org 全域
    # 被投毒到 Meta 的 IP），所以标注了「需能访问」。
    'tpl.config.geocoder.title': {
        'zh': '地名搜索',
        'en': 'Place search',
    },
    'tpl.config.geocoder.url': {
        'zh': '地理编码服务地址（geocoder_url）',
        'en': 'Geocoding service URL (geocoder_url)',
    },
    'tpl.config.geocoder.url_hint': {
        'zh': '留空 = 关闭地名搜索（出厂状态，程序不内置任何地名服务）。'
              '地址必须是 http/https 且**必须含 {q} 占位符**（关键词填进那里；'
              '少了它每次搜索都会返回同一批结果）。'
              '可直接粘贴的公共服务（免注册、免 key）：'
              'https://photon.komoot.io/api/?q={q} ；'
              'Nominatim 形态也支持：'
              'https://nominatim.openstreetmap.org/search?format=jsonv2&q={q}'
              '（需要本机能访问 openstreetmap.org）。'
              '自建或局域网服务同样可以 —— 请求由服务端发出，走上面的代理配置。'
              '公共服务都有使用频率限制、数据来自 OpenStreetMap（ODbL），'
              '请按各自的使用政策使用；要长期高频用请自建。',
        'en': 'Empty = place search is off (the factory state; no geocoder is '
              'bundled). The URL must be http/https and **must contain the {q} '
              'placeholder** (the keyword goes there; without it every search '
              'returns the same results). Public service you can paste as-is '
              '(no signup, no key): https://photon.komoot.io/api/?q={q} . '
              'Nominatim-shaped responses work too: '
              'https://nominatim.openstreetmap.org/search?format=jsonv2&q={q} '
              '(needs openstreetmap.org to be reachable from this machine). '
              'Self-hosted and LAN services are fine — the request is made by '
              'the server and follows the proxy settings above. Public '
              'instances are rate-limited and their data comes from '
              'OpenStreetMap (ODbL); follow each service\'s usage policy, and '
              'self-host for sustained heavy use.',
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
