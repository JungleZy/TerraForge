"""声明式参数的单一校验器。前端表单与后端路由共用这一份 schema，后端权威。"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Tuple

from src.plugins.protocols import ParamSchema

PARAM_TYPES = ('region', 'zoom_range', 'path', 'int', 'float', 'str',
               'bool', 'enum', 'credential')


def _coerce(spec, value):
    """(ok, coerced)。bool 只收 True/False/'true'/'false'/1/0——任意真值
    字符串静默吞掉配置错误是这个项目踩过的坑。"""
    if spec.type == 'int':
        # JSON 布尔是 int 子类（int(True) == 1）；层级/计数不含义布尔，与
        # src/services/geo_validation.py 的 coerce_number 同口径显式拒绝，
        # 本仓不许两套数值口径并存。
        if isinstance(value, bool):
            return False, None
        try:
            v = int(value)
        # inf 进 int() 抛 OverflowError。任何转换失败都只能进错误表，绝不能
        # 逃出本模块变成路由层的 500。
        except (TypeError, ValueError, OverflowError):
            return False, None
        if spec.min is not None and v < spec.min:
            return False, None
        if spec.max is not None and v > spec.max:
            return False, None
        return True, v
    if spec.type == 'float':
        if isinstance(value, bool):
            return False, None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, None
        # NaN 与任何数比较恒为 False，会整条穿透下面的 min/max 边界。
        if not math.isfinite(v):
            return False, None
        if spec.min is not None and v < spec.min:
            return False, None
        if spec.max is not None and v > spec.max:
            return False, None
        return True, v
    if spec.type == 'bool':
        if value in (True, 'true', 'True', 1):
            return True, True
        if value in (False, 'false', 'False', 0):
            return True, False
        return False, None
    if spec.type == 'enum':
        v = str(value)
        return (v in spec.choices), (v if v in spec.choices else None)
    # str / path / credential / region / zoom_range：结构化类型由调用方
    # （路由层）另行校验，这里只做非空与 str 化。
    v = value if isinstance(value, (dict, list)) else str(value)
    # None 已被 validate_params 的 null 守卫拦在门外，这里只需排空串。
    if spec.required and v == '':
        return False, None
    return True, v


def validate_params(schema: ParamSchema,
                    raw: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(清洗值, 错误表)。未知键报错——与 PUT /api/config 的 known_keys 闸门
    同一个理由：静默吞掉的键让用户以为设置生效了。"""
    if not isinstance(raw, Mapping):
        return {}, {'_': 'params must be an object'}
    clean: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    known = set(schema.keys())
    for key in raw:
        if key not in known:
            errors[key] = 'unknown param'
    for spec in schema.specs:
        # JSON null 等同未提供：前端把没填的可选字段序列化成 null 是常态，而
        # _coerce 的 str 分支会把 None 变成字面量 'None' 混进 clean——凭据字段
        # 拿着 'None' 去请求第三方是静默错误，本模块自称后端权威，调用方不该
        # 需要记得先洗一遍 null。
        if raw.get(spec.key) is None:
            if spec.required and spec.default is None:
                errors[spec.key] = 'required'
            elif spec.default is not None:
                clean[spec.key] = spec.default
            continue
        ok, value = _coerce(spec, raw[spec.key])
        if ok:
            clean[spec.key] = value
        else:
            errors[spec.key] = f'invalid {spec.type}'
    return clean, errors
