"""协议层的形状契约：dataclass 字段、Protocol 可运行时检查、API 版本。"""

from src.plugins.protocols import (
    API_MAJOR, PLUGIN_API_VERSION, Exporter, ParamSchema, ParamSpec,
    PipelinePlugin, PluginDefinition, PluginOutcome, SourceDescriptor,
    TaskEvent, TaskHook, ExportContext)


def test_api_version_shape():
    assert PLUGIN_API_VERSION == '1.0'
    assert API_MAJOR == PLUGIN_API_VERSION.split('.')[0]


def test_param_spec_defaults():
    p = ParamSpec(key='zoom', type='int', label='层级')
    assert p.required is True and p.choices == () and p.depends_on == {}


def test_plugin_outcome_values():
    assert {o.value for o in PluginOutcome} == {
        'completed', 'completed_with_gaps', 'pending_decision'}


def test_protocols_runtime_checkable():
    class FakePipeline:
        def params_schema(self): return ParamSchema(specs=())
        def estimate(self, params, region): return None
        def run(self, ctx): return PluginOutcome.COMPLETED

    class NotAPipeline: pass

    assert isinstance(FakePipeline(), PipelinePlugin)
    assert not isinstance(NotAPipeline(), PipelinePlugin)
    assert not isinstance(FakePipeline(), Exporter)


def test_plugin_definition_defaults_empty():
    d = PluginDefinition()
    assert d.sources == () and d.pipeline is None and d.exporters == () \
        and d.hooks == () and d.source_provider is None
