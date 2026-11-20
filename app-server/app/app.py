
import os
import re
import time
import random
import sqlite3

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, Response
)
from werkzeug.security import generate_password_hash, check_password_hash

from prometheus_client import (
    Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
)

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

DB_PATH = os.environ.get("DB_PATH", "/data/app.db")


# ---------- Prometheus Metrics ----------

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"]
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency in seconds",
    ["endpoint"]
)

LOGIN_ATTEMPTS = Counter(
    "login_attempts_total",
    "Login attempts",
    ["result"]
)

REGISTERED_USERS = Gauge(
    "registered_users_total",
    "Total registered users"
)

DB_QUERY_DURATION = Histogram(
    "db_query_duration_seconds",
    "DB query duration",
    ["operation"]
)


# ---------- OpenTelemetry Tracing ----------

provider = TracerProvider(
    resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "flask-app")
    })
)

provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter())
)

trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)


SQLite3Instrumentor().instrument()
FlaskInstrumentor().instrument_app(
    app,
    excluded_urls="metrics,health"
)


# ---------- Database ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def query(operation, sql, args=(), commit=False):
    start = time.time()

    cur = get_db().execute(sql, args)

    if commit:
        get_db().commit()

    DB_QUERY_DURATION.labels(operation).observe(
        time.time() - start
    )

    return cur


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)

    if db:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    db = sqlite3.connect(DB_PATH)

    db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL
        );
    """)

    REGISTERED_USERS.set(
        db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    )

    db.close()


init_db()


# ---------- Request Metrics ----------

@app.before_request
def start_timer():
    g.start = time.time()


@app.after_request
def record_metrics(resp):
    if request.path != "/metrics":
        endpoint = request.url_rule.rule if request.url_rule else "unmatched"

        REQUEST_COUNT.labels(
            request.method,
            endpoint,
            resp.status_code
        ).inc()

        REQUEST_LATENCY.labels(endpoint).observe(
            time.time() - g.start
        )

    return resp


# ---------- Pages ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            flash("Invalid email format.")
            return render_template("register.html"), 400

        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return render_template("register.html"), 400

        try:
            query(
                "insert_user",
                "INSERT INTO users(email, password_hash) VALUES(?,?)",
                (email, generate_password_hash(password)),
                commit=True
            )

        except sqlite3.IntegrityError:
            flash("Email already registered.")
            return render_template("register.html"), 409

        REGISTERED_USERS.inc()

        flash("Registered! Please log in.")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = query(
            "select_user",
            "SELECT * FROM users WHERE email=?",
            (email,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):

            app.logger.warning("failed login attempt for email=%s", email)
            LOGIN_ATTEMPTS.labels("success").inc()
            session["user_id"] = user["id"]

            return redirect(url_for("tasks"))

        LOGIN_ATTEMPTS.labels("failure").inc()

        flash("Invalid email or password.")
        return render_template("login.html"), 401

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/tasks", methods=["GET", "POST"])
def tasks():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        title = request.form.get("title", "").strip()

        if title:
            query(
                "insert_task",
                "INSERT INTO tasks(user_id, title) VALUES(?,?)",
                (session["user_id"], title),
                commit=True
            )

        return redirect(url_for("tasks"))

    rows = query(
        "select_tasks",
        "SELECT * FROM tasks WHERE user_id=?",
        (session["user_id"],)
    ).fetchall()

    return render_template("tasks.html", tasks=rows)


@app.route("/tasks/delete/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    query(
        "delete_task",
        "DELETE FROM tasks WHERE id=? AND user_id=?",
        (task_id, session["user_id"]),
        commit=True
    )

    return redirect(url_for("tasks"))


# ---------- Operational / Testing Endpoints ----------

@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/slow")
def slow():
    with tracer.start_as_current_span("simulated-latency"):
        time.sleep(random.uniform(0.5, 3))

    return {"status": "slow response done"}


@app.route("/error")
def error():
    return {"error": "simulated failure"}, 500


@app.route("/cpu-load")
def cpu_load():
    with tracer.start_as_current_span("cpu-burn"):
        end = time.time() + 5

        while time.time() < end:
            sum(i * i for i in range(10000))

    return {"status": "cpu burn done"}


# ---------- Prometheus Metrics Endpoint ----------

@app.route("/metrics")
def metrics():
    return Response(
        generate_latest(),
        mimetype=CONTENT_TYPE_LATEST
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

