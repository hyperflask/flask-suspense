# Flask-Suspense

Display loaders without requiring AJAX. Use [streaming templates](https://flask.palletsprojects.com/en/stable/patterns/streaming/#streaming-from-templates) to delay the rendering of parts of the template to the end of the response.

This is similar to [React's Suspense](https://react.dev/reference/react/Suspense) and [Next.js data loading mechanism](https://nextjs.org/docs/app/api-reference/file-conventions/loading).

## Installation

    pip install flask-suspense

## Usage

Enable the `Suspense` extension. Decorate your long running data loader using `@defer` and pass it to the template. Render the template using `flask_suspense.render_template`.

```py
from flask import Flask
from flask_suspense import Suspense, defer, render_template

app = Flask(__name__)
Suspense(app)

@app.route('/')
def index():

    @defer
    def data():
        time.sleep(2)  # Simulate a long-running process
        return "Loaded data"
    
    return render_template('index.html', data=data)
```

In your template, use the `{% suspense %}` block to defer rendering to the end of the template.

```jinja
{% suspense %}
    {{ data }}
{% endsuspense %}
```

You can also display a loading message:

```jinja
{% suspense %}
    {{ data }}
{% fallback %}
    <p>Loading...</p>
{% endsuspense %}
```

> [!TIP]
> The `{% suspense %}` block does not require the use of `@defer` loaders. It will simply delay
> the rendering of its content to the end of the template.

> [!TIP]
> Using `Flask.render_template()` will work as usual, suspense blocks will not be suspensed and rendered immediatly in place

> [!TIP]
> `flask_suspense.stream_template()` is also available

## Usage with Content Securiy Policy (CSP)

Using suspense blocks will use inline script tags. This may conflict with your [content security policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP).

A [nonce](https://developer.mozilla.org/fr/docs/Web/HTML/Reference/Global_attributes/nonce) can be provided to ensure the script tag is allowed. Set the nonce via `g.suspense_nonce`.

```
@app.before_request
def setup_nonce():
    g.suspense_nonce = "random string"

@app.after_request
def setup_csp(resp):
    resp.headers['Content-Security-Policy'] = f"script-src 'nonce-{g.suspense_once}';"
    return resp
```

## About the loader

A loading div will be inserted in place of your suspense block. This div has a `suspense-loader` class. It will be replaced once the content is loaded.

## How does it work ?

[Streaming responses](https://flask.palletsprojects.com/en/stable/patterns/streaming/) allow to start sending back to the response in multiple parts.

When rendering, `{% suspense %}` blocks are converted to macros and replaced by a loading div at the location they have been used.

The template is rendered in full first, without calling the suspense macros. It is sent back to the client. While the template is rendered, suspense blocks "register" themselves in the current rendering context.

"Registered" suspense macros are then called and their results are sent back wrapped in script tags that replace the loaders.

Registering macros allows us to catch the template they are defined in. This enables using suspense in includes as well.

`@defer` loaders ensures that the data loading will only start when the object is called as part of the macro, at the end of the stream.

## Customizing how content is inserted

When a suspense block is sent to the client, it replaces the loader using a simple `document.getElementById(suspenseId).outerHTML = suspenseBlockHtml`.

This can be customized by providing a custom function `window.__replace_suspense__` on the frontend.

```html
<script>
    window.__replace_suspense__ = (id, html) => {
        console.log(`Received suspense block ${id}`)
        document.getElementById(id).outerHTML = html;
    };
</script>
```