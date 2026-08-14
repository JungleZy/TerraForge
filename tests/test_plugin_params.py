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
