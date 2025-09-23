from jinja2 import Template
from jinja2.ext import Extension
from flask import current_app, stream_with_context, render_template as flask_render_template, stream_template as flask_stream_template, g
from flask.templating import before_render_template, template_rendered
from lazy_object_proxy import Proxy as defer
from blinker import Namespace
import uuid


_signals = Namespace()
suspense_before_render_template = _signals.signal('suspense_before_render_template')
suspense_before_render_blocks = _signals.signal('suspense_before_render_blocks')
suspense_after_render_blocks = _signals.signal('suspense_after_render_blocks')


class Suspense:
    def __init__(self, app=None, **kwargs):
        if app is not None:
            self.init_app(app, **kwargs)

    def init_app(self, app, always_stream=False, nonce_getter="g.get('suspense_nonce', '')"):
        app.jinja_env.add_extension(SuspenseExtension)
        app.jinja_env.suspense_auto_stream = always_stream
        app.jinja_env.suspense_nonce_getter = nonce_getter


def render_template(template_name_or_list, _suspense_disabled=False, **context):
    # this implementation of render_template() instead of always using stream_template()
    # ensures that the rendered template is sent as a single block and not streamed when it's not expected
    if _suspense_disabled:
        return flask_render_template(template_name_or_list, **context)
    
    g.suspense_stream = True
    suspense_before_render_template.send(current_app._get_current_object(), template=template_name_or_list, context=context)
    template, html, context = _render_template(template_name_or_list, **context)
    if "suspense-loader" not in html:
        return html
    return make_suspense_response(html, template, context)


def _render_template(template_name_or_list, **context):
    """Same as flask.render_template() but returns the template and updated context as well
    """
    app = current_app._get_current_object()
    template = app.jinja_env.get_or_select_template(template_name_or_list)
    app.update_template_context(context)
    before_render_template.send(
        app, _async_wrapper=app.ensure_sync, template=template, context=context
    )
    html = template.render(context)
    template_rendered.send(
        app, _async_wrapper=app.ensure_sync, template=template, context=context
    )
    return template, html, context


def stream_template(template_name_or_list, **context):
    return flask_stream_template(template_name_or_list, _suspense_stream=True, **context)


def make_suspense_response(html, template, context):
    @stream_with_context
    def stream():
        yield html
        for _, data in suspense_before_render_blocks.send(current_app._get_current_object(), template=template, context=context):
            if data:
                yield data
        yield from render_suspense_blocks(template, context)
        for _, data in suspense_after_render_blocks.send(current_app._get_current_object(), template=template, context=context):
            if data:
                yield data
    return stream()


def render_suspense_blocks(template, context):
    if not isinstance(template, Template):
        template = current_app.jinja_env.get_or_select_template(template)
    module = template.make_module(context)
    for attr in dir(module):
        if attr.startswith("suspense_") and callable(getattr(module, attr)):
            yield getattr(module, attr)()


class SuspenseExtension(Extension):
    def __init__(self, environment):
        super().__init__(environment)
        environment.extend(suspense_disabled=False,
                           suspense_nonce_getter='""',
                           suspense_auto_stream=True)

    def preprocess(self, source, name, filename=None):
        macro_defs = []
        macro_calls = []
        nonce_getter = self.environment.suspense_nonce_getter
        suspense_enabled_check = '{% if _suspense_disabled is undefined or _suspense_disabled is false %}'

        while True:
            block_start = source.find("{% suspense %}")
            if block_start == -1:
                break
            block_end = source.find("{% endsuspense %}", block_start)
            if block_end == -1:
                raise Exception("Missing endsuspense tag")
            body = source[block_start + 15:block_end]
            fallback = ""
            fallback_start = body.find("{% fallback %}")
            if fallback_start != -1:
                fallback = body[fallback_start + 15:]
                body = body[:fallback_start]

            if self.environment.suspense_disabled:
                source = source[:block_start] + body + source[block_end + 17:]
                continue
                
            id = str(uuid.uuid4()).split('-')[0]
            script = self.render_suspense_replace(id, body)
            macro = ("{% macro suspense_" + id + "() %}"
                     "<script nonce=\"{{ " + nonce_getter + " }}\">" + script + "</script>"
                     "{% endmacro %}\n")
            macro_call = "{{ suspense_" + id + "() }}"
            loader = '<div id="suspense-' + id + '" class="suspense-loader">' + fallback + '</div>'

            source = source[:block_start] + suspense_enabled_check + loader + '{% else %}' + macro_call + '{% endif %}' + source[block_end + 17:]
            macro_defs.append(macro)
            macro_calls.append(macro_call)

        source = "".join(macro_defs) + source # ensures macros are defined outside any blocks or calls

        if self.environment.suspense_disabled:
            return source

        macro_calls = suspense_enabled_check + "".join(macro_calls) + '{% endif %}'
        if not self.environment.suspense_auto_stream:
            return source + '{% if _suspense_stream is defined and _suspense_stream is true %}' + macro_calls + '{% endif %}'
        return source + macro_calls

    def render_suspense_replace(self, id, body):
        return f"(window.__replace_suspense__ || ((id, html) => document.getElementById(id).outerHTML = html))(\"suspense-{id}\", `{body}`)"