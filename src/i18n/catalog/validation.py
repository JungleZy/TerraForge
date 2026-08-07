"""src/services/geo_validation.py / tile_url_probe.py（同步校验反馈） 的界面文案。

key 命名：`validation.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # --- geo_validation：保存路径边界 -----------------------------------------
    'val.geo.output_path.must_be_absolute': {
        'zh': '保存路径必须是绝对路径（收到 {path}），可点输入框旁的「浏览」选择',
        'en': 'Save path must be absolute (got {path}); use the "Browse" button '
              'next to the input',
    },
    'val.geo.output_path.min_depth': {
        'zh': '保存路径至少需要两级目录（收到 {path}），不要直接选根目录/盘符根',
        'en': 'Save path needs at least two directory levels (got {path}); do not '
              'pick a filesystem or drive root',
    },

    # --- tile_url_probe：服务器条目校验 ---------------------------------------
    'val.tile_url.entry.empty': {
        'zh': '条目不能为空',
        'en': 'Entry cannot be empty',
    },
    'val.tile_url.entry.missing_host': {
        'zh': 'URL 缺少主机名',
        'en': 'URL is missing a host name',
    },
    'val.tile_url.entry.missing_placeholders': {
        'zh': '模板缺少占位符 {names}',
        'en': 'Template is missing placeholders {names}',
    },
    'val.tile_url.entry.unsupported_placeholders': {
        'zh': '模板包含不支持的占位符 {names}',
        'en': 'Template contains unsupported placeholders {names}',
    },
    'val.tile_url.entry.scheme_unsupported': {
        'zh': '只支持 http/https 协议',
        'en': 'Only the http/https scheme is supported',
    },
    'val.tile_url.entry.unknown_host': {
        'zh': '无法识别的主机/别名：{entry}',
        'en': 'Unrecognized host/alias: {entry}',
    },
    'val.tile_url.list.empty': {
        'zh': '瓦片服务器列表不能为空',
        'en': 'Tile server list cannot be empty',
    },
    'val.basemap.empty': {
        'zh': '底图源不能为空',
        'en': 'Basemap source cannot be empty',
    },
    'val.basemap.unknown': {
        'zh': '无法识别的底图源：{value}（应为预设名或完整 XYZ 模板）',
        'en': 'Unrecognized basemap source: {value} (expected a preset name or a full XYZ template)',
    },

    # --- tile_url_probe：通联探测结果 -----------------------------------------
    'val.tile_url.probe.empty_response': {
        'zh': '响应为空（0 字节）',
        'en': 'Empty response (0 bytes)',
    },
    'val.tile_url.probe.timeout': {
        'zh': '连接超时（{seconds}s）',
        'en': 'Connection timed out ({seconds}s)',
    },
    'val.tile_url.probe.connect_failed': {
        'zh': '连接失败：{error}',
        'en': 'Connection failed: {error}',
    },

    # --- tile_url_probe：并发数「测速推荐」 -----------------------------------
    'val.tile_url.recommend.no_servers': {
        'zh': '瓦片服务器列表为空，无法测速，给出保守值',
        'en': 'Tile server list is empty, cannot benchmark, falling back to a '
              'conservative value',
    },
    'val.tile_url.recommend.all_failed': {
        'zh': '测速样本全部失败（网络/代理不可用或瓦片不存在），给出保守值 {fallback}',
        'en': 'All benchmark samples failed (network/proxy unavailable, or tiles '
              'missing), falling back to conservative value {fallback}',
    },
    'val.tile_url.recommend.note_rising': {
        'zh': '实测最高 {best} 块/秒；推荐并发 {recommended}，且顶格仍在上升，可再手动调高试试',
        'en': 'Measured peak {best} tiles/s; recommended concurrency {recommended}, '
              'still rising at the top level, you can raise it further by hand',
    },
    'val.tile_url.recommend.note_knee': {
        'zh': '实测最高 {best} 块/秒；推荐并发 {recommended}（膝点：再加并发收益不足 10%）',
        'en': 'Measured peak {best} tiles/s; recommended concurrency {recommended} '
              '(knee point: more concurrency gains under 10%)',
    },
    'val.tile_url.recommend.error': {
        'zh': '测速出错（{error_type}），给出保守值 {fallback}',
        'en': 'Benchmark failed ({error_type}), falling back to conservative '
              'value {fallback}',
    },
}
