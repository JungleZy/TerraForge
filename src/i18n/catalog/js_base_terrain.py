"""static/js/base_terrain_status.js（底部状态栏的底图解压进度）的界面文案。

key 命名：`js.<模块>.<短名>`。只有 js.* 前缀会被内联进页面（window.__I18N__），
所以 JS 里要用的文案必须挂在这个前缀下。
"""

MESSAGES = {
    'js.base_unpack.running': {
        'zh': '底图解压 {percent}%',
        'en': 'Unpacking base terrain {percent}%',
    },
    'js.base_unpack.failed': {
        'zh': '底图不可用',
        'en': 'Base terrain unavailable',
    },
    # hover 出来的完整说明。用户看到「底图不可用」时唯一能据以行动的信息就是
    # {error}（多半是 assets/ 不可写），后半句告诉他这不影响切片本身。
    'js.base_unpack.failed_title': {
        'zh': '全球底图解压失败：{error}。地形切片仍可进行，但产物目录不会自包含。',
        'en': 'Base terrain unpack failed: {error}. Terrain tiling still works, '
              'but the artifact directory will not be self-contained.',
    },
}
