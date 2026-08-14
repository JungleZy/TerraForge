"""声明式参数校验：类型、必填、边界、枚举、缺省、未知键。"""

from src.plugins.params import PARAM_TYPES, validate_params
from src.plugins.protocols import ParamSchema, ParamSpec

SCHEMA = ParamSchema(specs=(
    ParamSpec(key='name', type='str', label='名称', default='未命名'),
    ParamSpec(key='zoom', type='int', label='层级', min=0, max=19),
    ParamSpec(key='ratio', type='float', label='比例', required=False, default=1.0),
    ParamSpec(key='mode', type='enum', label='模式', choices=('a', 'b')),
    ParamSpec(key='save', type='bool', label='保存', required=False, default=False),
))


def test_valid_payload_passes_with_defaults():
    clean, errors = validate_params(SCHEMA, {'zoom': '12', 'mode': 'a'})
    assert errors == {}
    assert clean['zoom'] == 12 and clean['name'] == '未命名' \
        and clean['ratio'] == 1.0 and clean['save'] is False


def test_missing_required_is_an_error():
    _, errors = validate_params(SCHEMA, {'mode': 'a'})
    assert 'zoom' in errors


def test_out_of_range_and_bad_enum():
    _, errors = validate_params(SCHEMA, {'zoom': 99, 'mode': 'zzz'})
    assert 'zoom' in errors and 'mode' in errors


def test_unknown_keys_are_rejected():
    _, errors = validate_params(SCHEMA, {'zoom': 3, 'mode': 'a', 'evil': 1})
    assert 'evil' in errors


def test_type_coercion_is_strict():
    _, errors = validate_params(SCHEMA, {'zoom': 'abc', 'mode': 'a'})
    assert 'zoom' in errors
    _, errors = validate_params(SCHEMA, {'zoom': 3, 'mode': 'a', 'save': 'yes'})
    assert 'save' in errors


def test_param_types_locked():
    assert set(PARAM_TYPES) == {
        'region', 'zoom_range', 'path', 'int', 'float', 'str', 'bool',
        'enum', 'credential'}


def test_json_null_is_treated_as_missing():
    """前端把没填的可选字段序列化成 null 是常态，不能变成字面量 'None'。"""
    clean, errors = validate_params(SCHEMA, {'zoom': 12, 'mode': 'a', 'name': None})
    assert errors == {} and clean['name'] == '未命名'   # 有缺省 → 回填
    _, errors = validate_params(SCHEMA, {'zoom': None, 'mode': 'a'})
    assert errors['zoom'] == 'required'                 # 必填无缺省 → required


def test_null_credential_does_not_become_the_string_none():
    """凭据/路径这类 str 系类型收到 null 时必须报 required，而不是拿到 'None'。"""
    schema = ParamSchema(specs=(
        ParamSpec(key='token', type='credential', label='令牌'),
        ParamSpec(key='out', type='path', label='输出目录'),
    ))
    clean, errors = validate_params(schema, {'token': None, 'out': None})
    assert clean == {}
    assert errors == {'token': 'required', 'out': 'required'}


def test_json_bool_is_not_a_number():
    """JSON 布尔是 int 子类；层级/比例不含义布尔，与 coerce_number 同口径显式拒绝。"""
    _, errors = validate_params(SCHEMA, {'zoom': True, 'mode': 'a'})
    assert errors['zoom'] == 'invalid int'
    _, errors = validate_params(SCHEMA, {'zoom': 3, 'mode': 'a', 'ratio': False})
    assert errors['ratio'] == 'invalid float'


def test_non_finite_numbers_are_rejected():
    """NaN 与任何比较都是 False，会整条穿透 min/max；inf 送进 int() 还会抛。"""
    for bad in (float('nan'), 'nan', float('inf'), '-inf'):
        _, errors = validate_params(SCHEMA, {'zoom': 3, 'mode': 'a', 'ratio': bad})
        assert errors == {'ratio': 'invalid float'}, bad
        # int 分支不许把 OverflowError 抛给路由层 —— 只能进错误表。
        _, errors = validate_params(SCHEMA, {'zoom': bad, 'mode': 'a'})
        assert errors == {'zoom': 'invalid int'}, bad


def test_nan_cannot_slip_past_bounds():
    bounded = ParamSchema(specs=(
        ParamSpec(key='ratio', type='float', label='比例', min=0.0, max=1.0),
    ))
    clean, errors = validate_params(bounded, {'ratio': float('nan')})
    assert clean == {} and errors == {'ratio': 'invalid float'}


def test_non_mapping_payload_is_rejected():
    """Task 5 路由层直接依赖这个契约：非对象入参不抛异常，只返回错误表。"""
    assert validate_params(SCHEMA, 'not a dict') == ({}, {'_': 'params must be an object'})
    assert validate_params(SCHEMA, None) == ({}, {'_': 'params must be an object'})
    assert validate_params(SCHEMA, [('zoom', 3)]) == ({}, {'_': 'params must be an object'})
