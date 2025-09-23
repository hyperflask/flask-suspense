from flask import Flask
from flask_suspense import Suspense, defer, render_template
import time


app = Flask(__name__)
Suspense(app)


@app.route("/")
def index():
    @defer
    def data():
        time.sleep(2)  # Simulate a long-running process
        return "Loaded data"

    return render_template("index.html", data=data)
