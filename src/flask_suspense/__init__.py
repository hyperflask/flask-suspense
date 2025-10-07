from jinja2 import nodes as jinja_nodes
from jinja2.ext import Extension
from flask import current_app, stream_with_context, render_template as flask_render_template, stream_template as flask_stream_template, g
from lazy_object_proxy import Proxy
from blinker import Namespace
import uuid
import inspect


_signals = Namespace()
suspense_before_render_template = _signals.signal('suspense_before_render_template')
suspense_before_render_macros = _signals.signal('suspense_before_render_macros')
suspense_after_render_macros = _signals.signal('suspense_after_render_macros')


class Suspense:
    def __init__(self, app=None, **kwargs):
        if app is not None:
            self.init_app(app, **kwargs)

    def init_app(self, app, nonce_getter="g.get('suspense_nonce', '')"):
        app.jinja_env.add_extension(SuspenseExtension)
        app.jinja_env.suspense_nonce_getter = nonce_getter


def render_template(template_name_or_list, **context):
    # this implementation of render_template(), instead of always using stream_template(), ensures
    # that the rendered template is sent as a single block and not streamed when it's not expected
    suspense_before_render_template.send(current_app._get_current_object(), template=template_name_or_list, context=context)

    template = current_app.jinja_env.get_or_select_template(template_name_or_list)
    template_render = template.render
    saved_context = None
    def render(context):
        # we monkey patch the template.render method to save the full context
        # (flask calls app.update_template_context() in render_template())
        # the goal is not to reimplement render_template() fully
        nonlocal saved_context
        saved_context = context
        return template_render(context)
    template.render = render

    g.suspense_enabled = True
    g.suspense_macros = {} # we use g as we want the variable to be accessible across all templates (including inside macros included from other templates)
    html = flask_render_template(template, **context)
    g.suspense_enabled = False
    if not g.suspense_macros:
        return html
    return make_suspense_response(html, template, saved_context, g.suspense_macros)


def stream_template(template_name_or_list, **context):
    # implementation of stream_template() is simpler but will call app.update_template_context() twice
    # as we don't intercept the context like in render_template()
    suspense_before_render_template.send(current_app._get_current_object(), template=template_name_or_list, context=context)
    template = current_app.jinja_env.get_or_select_template(template_name_or_list)
    g.suspense_enabled = True
    g.suspense_macros = {}
    stream = flask_stream_template(template, **context)
    current_app.update_template_context(context)
    return make_suspense_response(stream, template, context, g.suspense_macros)


def make_suspense_response(rv, template, context, suspense_macros):
    @stream_with_context
    def stream():
        if inspect.isgenerator(rv):
            yield from rv # handle stream_template()
        else:
            yield rv
        for _, data in suspense_before_render_macros.send(current_app._get_current_object(), template=template, context=context):
            if data:
                yield data

        g.suspense_enabled = False # needed because streaming won't have set it to false

        # call suspense macros that were registered in the rendering phase
        # these macros can come from different templates
        for macro_template, (ctx, macros) in suspense_macros.items():
            module = current_app.jinja_env.get_template(macro_template).make_module(ctx)
            for macro in macros:
                yield getattr(module, macro)()

        for _, data in suspense_after_render_macros.send(current_app._get_current_object(), template=template, context=context):
            if data:
                yield data

    return stream(), {"x-suspense": "1"}


def defer(func, *args, **kwargs):
    return Proxy(lambda: func(*args, **kwargs))


class SuspenseExtension(Extension):
    tags = {'register_suspense_macro'}

    def __init__(self, environment):
        super().__init__(environment)
        environment.extend(suspense_disabled=False,
                           suspense_nonce_getter='""')

    def preprocess(self, source, name, filename=None):
        macro_defs = []
        macro_calls = []
        nonce_getter = self.environment.suspense_nonce_getter
        
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
            loader = self.render_loader(id, fallback) + self.render_register(id) # register the suspense macros when it is enabled (ie. we display the loader)

            source = source[:block_start] + self.render_enabled_check(loader, else_=body) + source[block_end + 17:]
            macro_defs.append(macro)
            macro_calls.append(macro_call)

        if self.environment.suspense_disabled or not macro_defs:
            return source

        # putting macros at beginning ensures macros are defined outside any macros or calls (thus accessible on the template module)
        return "".join(macro_defs) + source
    
    def render_enabled_check(self, body, else_=None):
        return '{% if g.suspense_enabled %}' + body + ('{% else %}' + else_ if else_ is not None else '') + '{% endif %}'
    
    def render_loader(self, id, body):
        return f"<div id=\"suspense-{id}\" class=\"suspense-loader\">{body}</div>"
    
    def render_register(self, id):
        return "{% if g.suspense_enabled and g.suspense_macros is defined %}{% register_suspense_macro(g.suspense_macros, 'suspense_" + id + "') %}{% endif %}"

    def render_suspense_replace(self, id, body):
        return f"(window.__replace_suspense__ || ((id, html) => document.getElementById(id).outerHTML = html))(\"suspense-{id}\", `{body}`)"
    
    def parse(self, parser):
        lineno = next(parser.stream).lineno
        args, kwargs, _, __ = parser.parse_call_args()
        context = jinja_nodes.ContextReference()
        return jinja_nodes.Output([self.call_method("register_suspense_macro", [context] + args, kwargs, lineno=lineno)])
    
    def register_suspense_macro(self, ctx, macros_registry, macro_name):
        # register the template name and the full ctx for later rendering
        macros_registry.setdefault(ctx.name, [ctx.get_all(), []])[1].append(macro_name)
        return ""