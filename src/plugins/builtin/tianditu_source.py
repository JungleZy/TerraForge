"""天地图数据源（影像 + 注记）。纯数据插件：本模块零逻辑。

WMTS GetTile 的 `tk` 是 query token，恰好落在宿主的 `{credential}` 占位符
机制上（download_engine.get_tile_url 在发请求前那一瞬替换），所以这里不需要
实现 SourceProvider——两个 SourceDescriptor 就是全部。

凭据只走 config_json：`credential_key='token'` → registry.build_source_snapshot
把它转成 `credential_reference='plugin:tianditu:token'`，真值留在
plugins.config_json 里。快照、任务行、指纹、日志里都只有键名，因此换 token
不改指纹、已下瓦片不失效。**本模块任何地方都不得出现 token 真值**，也不得把
它塞进任务参数——参数会进任务行与日志。

用户配置：插件管理页 → tianditu → config → {"token": "<天地图 key>"}。
key 在 https://console.tianditu.gov.cn/ 申请。
"""

from src.plugins.protocols import PluginDefinition, SourceDescriptor

MANIFEST = {
    'id': 'tianditu',
    'name': '天地图数据源',
    'version': '1.0.0',
    'api_version': '1',
    'capabilities': ['sources'],
    'permissions': ['network'],
    'description': '天地图影像（img_w）与注记（cia_w），WMTS RESTful，'
                   '需要在插件配置里填 token。',
}

#: WMTS GetTile 的 KVP 形式。双花括号是 str.format 的转义——`_descriptor`
#: 只替换 {layer}/{layer_code}，产出的模板里 {s}/{z}/{y}/{x}/{credential}
#: 是留给宿主的字面量占位符。TILEMATRIX=z、TILEROW=y、TILECOL=x（行是纬向，
#: 列是经向，写反了会拿到转置的瓦片）。
_Template = ('https://t{{s}}.tianditu.gov.cn/{layer}/wmts'
             '?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0'
             '&LAYER={layer_code}&STYLE=default&TILEMATRIXSET=w'
             '&TILEMATRIX={{z}}&TILEROW={{y}}&TILECOL={{x}}'
             '&FORMAT=tiles&tk={{credential}}')

#: t0~t7 八个负载均衡子域，由宿主按 server_index 轮换。
_SUBDOMAINS = tuple(str(i) for i in range(8))

_ATTRIBUTION = '© 天地图 · 国家基础地理信息中心'

#: 随 SourceSnapshot 进产物，也在下载弹窗里显示给用户。写实：天地图按 key
#: 计配额（额度随账号类型不同，见控制台），服务条款要求标注来源，批量下载与
#: 离线转存属于需要确认账号权限的用法。
_USAGE_POLICY = (
    '天地图为实名注册服务：每个 key 有每日调用配额（额度随账号类型不同，'
    '见 https://console.tianditu.gov.cn/ 控制台），超限返回错误而非瓦片。'
    '使用须保留“天地图 · 国家基础地理信息中心”来源标注；批量下载与离线'
    '转存前请先确认本账号的授权范围，并遵守天地图服务条款。')


def _descriptor(source_id: str, name: str,
                layer: str, layer_code: str) -> SourceDescriptor:
    return SourceDescriptor(
        source_id=source_id,
        name=name,
        url_template=_Template.format(layer=layer, layer_code=layer_code),
        max_zoom=18,
        attribution=_ATTRIBUTION,
        usage_policy=_USAGE_POLICY,
        subdomains=_SUBDOMAINS,
        credential_key='token',
    )


def register() -> PluginDefinition:
    return PluginDefinition(sources=(
        _descriptor('img', '天地图影像', 'img_w', 'img'),
        _descriptor('cia', '天地图注记', 'cia_w', 'cia'),
    ))
