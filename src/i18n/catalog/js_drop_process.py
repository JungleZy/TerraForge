"""static/js/drop_process.js 的界面文案。"""

MESSAGES = {
    # 提示要同时说清两类文件：这个投放区既收高程文件（打开本地处理），也收边界
    # 矢量（当作下载区域）。只说一类会让用户以为另一类不支持。
    'js.drop.hint': {
        'zh': '松开鼠标：高程文件打开本地处理，边界文件作为下载区域',
        'en': 'Drop to open local processing for elevation files, or use a '
              'boundary file as the download region',
    },
    'js.drop.unsupported': {
        'zh': '不支持这些文件。高程文件请用 .tif / .tiff，下载区域请用 .geojson / .json / .kml / .kmz / .zip / .shp',
        'en': 'Unsupported files. Use .tif / .tiff for elevation files, or '
              '.geojson / .json / .kml / .kmz / .zip / .shp for a download region',
    },
    'js.drop.failed': {
        'zh': '无法读取拖入的文件',
        'en': 'Could not read the dropped files',
    },
}
