"""Flask 网页应用：限发损失归因入仓。"""

from __future__ import annotations

import os
import secrets
import sqlite3
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import service
from .db import connect, init_db
from .parsing import day_bounds, parse_meter_csv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "instance" / "curtailment.sqlite3"


def db_path() -> Path:
    return Path(os.environ.get("CURTAILMENT_DB", str(DEFAULT_DB)))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
    app.config.update(
        SECRET_KEY=os.environ.get("CURTAILMENT_SECRET", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    path = app.config.get("DATABASE") or db_path()
    app.config["DATABASE"] = str(path)
    init_db(path)

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def csrf_guard():
        if request.method == "POST":
            token = session.get("csrf")
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
            if not token or not sent or not secrets.compare_digest(token, sent):
                abort(400, "CSRF 校验失败，请刷新页面后重试")

    def csrf() -> str:
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            session["csrf"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        return response

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/")
    def index():
        conn = get_db()
        turbines = service.list_turbines(conn)
        selected = request.args.get("turbine") or (turbines[0] if turbines else None)
        days = service.list_days(conn, selected) if selected else []
        orders = service.list_orders(conn)
        paused = [o for o in orders if o["conflicted"]]
        return render_template(
            "index.html",
            turbines=turbines,
            selected=selected,
            days=days,
            orders=orders[:20],
            order_count=len(orders),
            paused=paused,
        )

    @app.post("/import")
    def import_meter():
        conn = get_db()
        turbine = (request.form.get("turbine") or "").strip()
        upload = request.files.get("file")
        replace = request.form.get("replace") == "on"
        if upload is None or not upload.filename:
            flash("请选择要导入的 CSV 文件", "error")
            return redirect(url_for("index"))
        try:
            parsed = parse_meter_csv(upload.read(), default_turbine=turbine)
            result = service.import_meter(conn, parsed.turbine, parsed.rows, replace=replace)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index", turbine=turbine or None))
        flash(
            f"导入 {result.turbine} 成功：新增 {result.inserted} 格，更新 {result.updated} 格，"
            f"重算日期 {', '.join(result.dates)}",
            "success",
        )
        for warning in parsed.warnings:
            flash(warning, "warning")
        for order_no, hits in sorted(result.order_conflicts.items()):
            flash(
                f"单据 {order_no} 因导入数据出现 {len(hits)} 个冲突格（实发量超上限），"
                f"已暂停参与归因，请核对或更正。",
                "error",
            )
        return redirect(url_for("index", turbine=result.turbine))

    @app.get("/day/<turbine>/<date>")
    def day_detail(turbine: str, date: str):
        conn = get_db()
        try:
            day_bounds(date)
        except (ValueError, TypeError):
            abort(404)
        day = service.get_day(conn, turbine, date)
        if day is None:
            flash(f"{turbine} 在 {date} 没有归因结果（可能未导入计量数据）", "warning")
            return redirect(url_for("index", turbine=turbine))
        return render_template("day.html", day=day)

    @app.get("/orders")
    def orders():
        conn = get_db()
        return render_template(
            "orders.html",
            orders=service.list_orders(conn),
            prefill=request.args,
        )

    @app.get("/orders/<order_no>")
    def order_detail(order_no: str):
        conn = get_db()
        order = service.get_order(conn, order_no)
        if order is None:
            abort(404)
        return render_template("order.html", order=order)

    @app.post("/orders")
    def submit_order():
        conn = get_db()
        form = request.form
        try:
            data = service.OrderInput.from_form(form)
        except service.ValidationError as exc:
            for message in exc.errors:
                flash(message, "error")
            return (
                render_template("orders.html", orders=service.list_orders(conn), prefill=form),
                400,
            )

        try:
            result = service.submit_order(conn, data)
        except service.OrderConflict as exc:
            return (
                render_template(
                    "order_conflict.html",
                    order_no=exc.order_no,
                    conflicts=exc.conflicts,
                    prefill=form,
                ),
                409,
            )
        except service.ValidationError as exc:
            for message in exc.errors:
                flash(message, "error")
            return (
                render_template("orders.html", orders=service.list_orders(conn), prefill=form),
                400,
            )

        action = "更正" if result.corrected else "新建"
        flash(
            f"单据 {result.order_no} {action}生效（修订 r{result.revision}），"
            f"已重算日期：{', '.join(result.dates) or '无'}",
            "success",
        )
        return redirect(url_for("order_detail", order_no=result.order_no))

    @app.errorhandler(400)
    def bad_request(exc):
        return (
            render_template(
                "error.html", code=400, message=getattr(exc, "description", "请求无效")
            ),
            400,
        )

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", code=404, message="页面或资源不存在"), 404

    return app


def main() -> None:
    app = create_app()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1")


if __name__ == "__main__":
    main()
