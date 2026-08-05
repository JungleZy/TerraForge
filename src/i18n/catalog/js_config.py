"""static/js/config.js（配置页交互） 的界面文案。

key 命名：`js.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # --- 缓存管理 ---
    'js.config.cache.all_label': {
        'zh': '全部缓存',
        'en': 'All caches',
    },
    'js.config.cache.load_failed': {
        'zh': '加载失败：{error}',
        'en': 'Load failed: {error}',
    },
    'js.config.cache.empty': {
        'zh': '暂无缓存',
        'en': 'No cache',
    },
    'js.config.cache.size_files': {
        'zh': '{size} · {count} 个文件',
        'en': '{size} · {count} files',
    },
    'js.config.cache.clear': {
        'zh': '清理',
        'en': 'Clear',
    },
    # {warning} 为空或以一个空格打头，与原来的字符串拼接保持一致
    'js.config.cache.clear_confirm': {
        'zh': '将删除「{label}」的缓存（{size}）。{warning}',
        'en': 'This will delete the "{label}" cache ({size}).{warning}',
    },
    'js.config.cache.dem_warning': {
        'zh': '注意：DEM 缓存重新下载需要 Earthdata 账号登录。',
        'en': 'Note: re-downloading the DEM cache requires an Earthdata login.',
    },
    'js.config.cache.clear_title': {
        'zh': '清理缓存',
        'en': 'Clear cache',
    },
    'js.config.cache.continue': {
        'zh': '继续',
        'en': 'Continue',
    },
    'js.config.cache.clear_confirm_again': {
        'zh': '再次确认：删除「{label}」后不可恢复，确定删除？',
        'en': 'Confirm again: deleting "{label}" cannot be undone. Delete it?',
    },
    'js.config.cache.confirm_again_title': {
        'zh': '再次确认',
        'en': 'Confirm again',
    },
    'js.config.cache.confirm_delete': {
        'zh': '确认删除',
        'en': 'Confirm delete',
    },
    'js.config.cache.tasks_running': {
        'zh': '有任务尚未结束。',
        'en': 'Some tasks are still running.',
    },
    'js.config.cache.force_confirm': {
        'zh': '{error} 仍要清理吗？',
        'en': '{error} Clear anyway?',
    },
    'js.config.cache.tasks_running_title': {
        'zh': '有任务未结束',
        'en': 'Tasks still running',
    },
    'js.config.cache.force_clear': {
        'zh': '仍然清理',
        'en': 'Clear anyway',
    },
    'js.config.cache.clear_cancelled': {
        'zh': '已取消清理',
        'en': 'Clearing cancelled',
    },
    'js.config.cache.cleared': {
        'zh': '已清理「{label}」，释放 {size}',
        'en': 'Cleared "{label}", freed {size}',
    },
    'js.config.cache.clear_failed': {
        'zh': '清理失败: {error}',
        'en': 'Clear failed: {error}',
    },

    # --- 并发下载数：测速推荐 ---
    'js.config.concurrency.testing': {
        'zh': '测速中…',
        'en': 'Testing…',
    },
    'js.config.concurrency.testing_hint': {
        'zh': '正在按当前网络环境实测吞吐，约 30 秒…',
        'en': 'Measuring throughput on the current network, about 30 seconds…',
    },
    'js.config.concurrency.recommended': {
        'zh': '推荐 {n}',
        'en': 'Recommended {n}',
    },
    'js.config.concurrency.filled': {
        'zh': '{note}（已填入，保存后生效）',
        'en': '{note} (filled in, takes effect after saving)',
    },
    'js.config.concurrency.failed': {
        'zh': '推荐失败：{error}',
        'en': 'Recommendation failed: {error}',
    },

    # --- 瓦片服务器列表编辑器 ---
    'js.config.tile.verify': {
        'zh': '验证',
        'en': 'Verify',
    },
    # 进 HTML 属性（aria-label / title），译文里不要出现引号
    'js.config.tile.remove': {
        'zh': '删除该服务器',
        'en': 'Remove this server',
    },
    'js.config.tile.verifying': {
        'zh': '正在验证…',
        'en': 'Verifying…',
    },
    'js.config.tile.unknown_type': {
        'zh': '未知类型',
        'en': 'unknown type',
    },
    'js.config.tile.verify_ok': {
        'zh': '通联正常 · HTTP {status} · {content_type} · {elapsed}ms（样例瓦片 {tile}）',
        'en': 'Reachable · HTTP {status} · {content_type} · {elapsed}ms '
              '(sample tile {tile})',
    },
    'js.config.tile.verify_failed': {
        'zh': '验证失败：{error}',
        'en': 'Verification failed: {error}',
    },

    # --- 保存 / 重置 ---
    'js.config.save.ok': {
        'zh': '配置保存成功！',
        'en': 'Configuration saved!',
    },
    # 多条校验错误的连接符
    'js.config.save.error_sep': {
        'zh': '；',
        'en': '; ',
    },
    'js.config.save.failed': {
        'zh': '保存失败: {error}',
        'en': 'Save failed: {error}',
    },
    'js.config.reset.confirm': {
        'zh': '确定要重置所有配置为默认值吗？',
        'en': 'Reset all settings to their defaults?',
    },
    'js.config.reset.title': {
        'zh': '重置配置',
        'en': 'Reset configuration',
    },
    'js.config.reset.ok': {
        'zh': '已重置为默认配置',
        'en': 'Reset to default configuration',
    },
    'js.config.reset.failed': {
        'zh': '重置失败: {error}',
        'en': 'Reset failed: {error}',
    },
}
