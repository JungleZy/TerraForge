"""声明式参数的单一校验器。前端表单与后端路由共用这一份 schema，后端权威。"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from src.plugins.protocols import ParamSchema

PARAM_TYPES = ('region', 'zoom_range', 'path', 'int', 'float', 'str',
               'bool', 'enum', 'credential')


def _coerce(spec, value):
    """(ok, coerced)。bool 只收 True/False/'true'/'false'/1/0——任意真值
    字符串静默吞掉配置错误是这个项目踩过的坑。"""
    if spec.type == 'int':
        try:
            v = int(value)
        except (TypeError, ValueError):
            return False, None
        if spec.min is not None and v < spec.min:
            return False, None
        if spec.max is not None and v > spec.max:
            return False, None
        return True, v
    if spec.type == 'float':
        try:
            v = float(value)
        except (TypeError, ValueError):
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
    if spec.required and (v == '' or v is None):
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
        if spec.key not in raw:
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
