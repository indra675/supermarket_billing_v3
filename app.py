"""
Counter v3 - Supermarket Billing System (backend)
Flask REST API + SQLite

v3 adds: cashier/admin login (hashed passwords, sessions, lockout),
role-based permissions, stock tracking with an audit ledger, oversell
protection, low-stock alerts and per-cashier sales reporting.
"""
import csv
import io
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, g, jsonify, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "billing.db"))

GST_RATES = (0, 5, 12, 18, 28)
PAYMENT_METHODS = ("Cash", "UPI", "Card")

# ---- policy knobs (override with environment variables) ----
MAX_CASHIER_DISCOUNT = float(os.environ.get("MAX_CASHIER_DISCOUNT_PCT", 10))
CASHIER_CUSTOM_ITEMS = os.environ.get("CASHIER_CUSTOM_ITEMS", "1") == "1"
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300
MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")


def _load_secret_key():
    """SECRET_KEY env var wins; otherwise create and reuse a local key file."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
        except OSError:
            pass
        return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
if os.environ.get("TRUST_PROXY") == "1":  # set on Render/Heroku so remote_addr is the real client
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ValidationError(Exception):
    pass


class NotFoundError(Exception):
    pass


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    # isolation_level=None -> we control transactions explicitly (see write_tx)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def write_tx():
    """One atomic write transaction. BEGIN IMMEDIATE takes the write lock up
    front, so two cashiers can never both read the same stock and oversell."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, col, ddl):
    if col not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        return True
    return False


def init_db():
    """Create tables, then add any columns an older (v1/v2) database lacks."""
    conn = get_db()
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            total REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bill_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            price REAL NOT NULL,
            unit TEXT,
            quantity REAL NOT NULL,
            item_total REAL NOT NULL,
            FOREIGN KEY (bill_id) REFERENCES bills (id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT UNIQUE,
            price REAL NOT NULL,
            unit TEXT,
            gst_rate REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'cashier')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            change REAL NOT NULL,
            balance REAL NOT NULL,
            reason TEXT NOT NULL,
            bill_id INTEGER,
            user_id INTEGER,
            username TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    # --- v2 migrations ---
    for col, ddl in [
        ("subtotal", "REAL NOT NULL DEFAULT 0"),
        ("discount_type", "TEXT NOT NULL DEFAULT 'percent'"),
        ("discount_value", "REAL NOT NULL DEFAULT 0"),
        ("discount_amount", "REAL NOT NULL DEFAULT 0"),
        ("tax", "REAL NOT NULL DEFAULT 0"),
        ("payment_method", "TEXT NOT NULL DEFAULT 'Cash'"),
    ]:
        if _add_column(conn, "bills", col, ddl) and col == "subtotal":
            conn.execute("UPDATE bills SET subtotal = total")
    _add_column(conn, "bill_items", "gst_rate", "REAL NOT NULL DEFAULT 0")
    # --- v3 migrations ---
    _add_column(conn, "bills", "cashier_id", "INTEGER")
    _add_column(conn, "bills", "cashier_name", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "bill_items", "product_id",
                "INTEGER REFERENCES products(id) ON DELETE SET NULL")
    # stock NULL = "not tracked" (so every v2 product keeps selling as before)
    _add_column(conn, "products", "stock", "REAL")
    _add_column(conn, "products", "reorder_level", "REAL NOT NULL DEFAULT 0")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_bill ON bill_items(bill_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bills_created ON bills(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bills_cashier ON bills(cashier_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moves_product ON stock_movements(product_id)")
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def body():
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def num(value, label, allow_zero=True, allow_negative=False):
    try:
        v = float(value if value not in (None, "") else 0)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} must be a number.")
    if not math.isfinite(v):
        raise ValidationError(f"{label} must be a number.")
    if v < 0 and not allow_negative:
        raise ValidationError(f"{label} can't be negative.")
    if v == 0 and not allow_zero:
        raise ValidationError(f"{label} must be greater than zero.")
    return v


def fmt_qty(x):
    return f"{round(x, 3):g}"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
_failed = {}  # "user|ip" -> {"n": failures, "until": lockout-expiry}
_DUMMY_HASH = generate_password_hash("not-a-real-password")  # equalises login timing


def lockout_remaining(key):
    rec = _failed.get(key)
    if rec and rec["until"] > time.time():
        return rec["until"] - time.time()
    return 0


def record_failure(key):
    if len(_failed) > 2000:  # keep memory bounded
        now = time.time()
        for k in [k for k, v in _failed.items() if v["until"] < now and v["n"] == 0]:
            _failed.pop(k, None)
    rec = _failed.setdefault(key, {"n": 0, "until": 0})
    rec["n"] += 1
    if rec["n"] >= MAX_FAILED_LOGINS:
        rec["until"] = time.time() + LOCKOUT_SECONDS
        rec["n"] = 0


def current_user():
    if "_user" in g:
        return g._user
    user = None
    uid = session.get("uid")
    if uid:
        conn = get_db()
        row = conn.execute(
            "SELECT id, username, role, active FROM users WHERE id = ?", (uid,)
        ).fetchone()
        conn.close()
        if row and row["active"]:
            user = {"id": row["id"], "username": row["username"], "role": row["role"]}
        else:
            session.clear()  # deactivated or deleted -> kicked out immediately
    g._user = user
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not current_user():
            return jsonify({"error": "Please sign in."}), 401
        return fn(*a, **k)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        user = current_user()
        if not user:
            return jsonify({"error": "Please sign in."}), 401
        if user["role"] != "admin":
            return jsonify({"error": "Admin access required."}), 403
        return fn(*a, **k)
    return wrapper


def validate_username(name):
    if not USERNAME_RE.match(name or ""):
        raise ValidationError("Username must be 3-30 characters: letters, numbers, . _ -")
    return name


def validate_password(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD_LEN:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    return pw


def public_config():
    return {"maxCashierDiscount": MAX_CASHIER_DISCOUNT, "cashierCustomItems": CASHIER_CUSTOM_ITEMS}


@app.route("/api/auth/me")
def auth_me():
    user = current_user()
    conn = get_db()
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    conn.close()
    return jsonify({"user": user, "needsSetup": not has_users,
                    "config": public_config() if user else None})


@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    """First run only: create the initial admin account."""
    data = body()
    try:
        username = validate_username((data.get("username") or "").strip())
        password = validate_password(data.get("password"))
        with write_tx() as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return jsonify({"error": "Setup is already done. Please sign in."}), 403
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
                (username, generate_password_hash(password), "admin", now_iso()))
            uid = cur.lastrowid
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    session.clear()
    session["uid"] = uid
    session.permanent = True
    return jsonify({"user": {"id": uid, "username": username, "role": "admin"},
                    "config": public_config()}), 201


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = body()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    key = f"{username.lower()}|{request.remote_addr}"

    wait = lockout_remaining(key)
    if wait:
        return jsonify({"error": f"Too many failed attempts. Try again in {math.ceil(wait / 60)} min."}), 429

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
    if not (row and ok and row["active"]):
        record_failure(key)
        return jsonify({"error": "Wrong username or password."}), 401

    _failed.pop(key, None)
    session.clear()
    session["uid"] = row["id"]
    session.permanent = True
    return jsonify({"user": {"id": row["id"], "username": row["username"], "role": row["role"]},
                    "config": public_config()})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/password", methods=["POST"])
@login_required
def auth_change_password():
    data = body()
    user = current_user()
    try:
        new_pw = validate_password(data.get("new"))
        with write_tx() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not check_password_hash(row["password_hash"], data.get("current") or ""):
                raise ValidationError("Current password is wrong.")
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(new_pw), user["id"]))
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin: user management
# ---------------------------------------------------------------------------
def user_row_to_dict(r):
    return {"id": r["id"], "username": r["username"], "role": r["role"],
            "active": bool(r["active"]), "createdAt": r["created_at"], "bills": r["bills"]}


USER_SELECT = ("SELECT u.*, (SELECT COUNT(*) FROM bills b WHERE b.cashier_id = u.id) AS bills "
               "FROM users u")


@app.route("/api/users", methods=["GET"])
@admin_required
def list_users():
    conn = get_db()
    rows = conn.execute(USER_SELECT + " ORDER BY u.role, u.username COLLATE NOCASE").fetchall()
    conn.close()
    return jsonify([user_row_to_dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    data = body()
    try:
        username = validate_username((data.get("username") or "").strip())
        password = validate_password(data.get("password"))
        role = data.get("role")
        if role not in ("admin", "cashier"):
            raise ValidationError("Role must be admin or cashier.")
        with write_tx() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
                    (username, generate_password_hash(password), role, now_iso()))
            except sqlite3.IntegrityError:
                return jsonify({"error": "That username is taken."}), 409
            row = conn.execute(USER_SELECT + " WHERE u.id = ?", (cur.lastrowid,)).fetchone()
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(user_row_to_dict(row)), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@admin_required
def update_user(uid):
    data = body()
    me = current_user()
    try:
        with write_tx() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not row:
                raise NotFoundError()
            fields, params = [], []
            if "role" in data:
                if data["role"] not in ("admin", "cashier"):
                    raise ValidationError("Role must be admin or cashier.")
                if uid == me["id"] and data["role"] != row["role"]:
                    raise ValidationError("You can't change your own role.")
                fields.append("role = ?"); params.append(data["role"])
            if "active" in data:
                if uid == me["id"] and not data["active"]:
                    raise ValidationError("You can't deactivate your own account.")
                fields.append("active = ?"); params.append(1 if data["active"] else 0)
            if data.get("password"):
                fields.append("password_hash = ?")
                params.append(generate_password_hash(validate_password(data["password"])))
            if not fields:
                raise ValidationError("Nothing to update.")
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", (*params, uid))
            out = conn.execute(USER_SELECT + " WHERE u.id = ?", (uid,)).fetchone()
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    except NotFoundError:
        return jsonify({"error": "User not found."}), 404
    return jsonify(user_row_to_dict(out))


# ---------------------------------------------------------------------------
# Stock ledger
# ---------------------------------------------------------------------------
def log_movement(conn, pid, change, balance, reason, bill_id, user, note=None):
    conn.execute(
        """INSERT INTO stock_movements
           (product_id, change, balance, reason, bill_id, user_id, username, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pid, change, balance, reason, bill_id, user["id"] if user else None,
         user["username"] if user else None, note, now_iso()))


def adjust_stock(conn, old, new, reason, bill_id, user):
    """Move stock from the quantities a bill used to hold (`old`) to what it
    holds now (`new`); both are {product_id: qty}. Untracked products (stock
    IS NULL) are skipped. Raises ValidationError if stock would go negative."""
    for pid in sorted(set(old) | set(new)):
        o, n = old.get(pid, 0.0), new.get(pid, 0.0)
        if abs(n - o) < 1e-9:
            continue
        row = conn.execute("SELECT name, unit, stock FROM products WHERE id = ?", (pid,)).fetchone()
        if row is None or row["stock"] is None:
            continue
        available = row["stock"] + o  # stock plus what this bill already took
        if n > available + 1e-9:
            raise ValidationError(
                f"Not enough stock for '{row['name']}': only {fmt_qty(available)} "
                f"{row['unit'] or 'units'} available.")
        balance = round(available - n, 3)
        conn.execute("UPDATE products SET stock = ? WHERE id = ?", (balance, pid))
        log_movement(conn, pid, round(o - n, 3), balance, reason, bill_id, user)


def bill_stock_map(conn, bill_id):
    rows = conn.execute(
        "SELECT product_id, SUM(quantity) AS q FROM bill_items "
        "WHERE bill_id = ? AND product_id IS NOT NULL GROUP BY product_id", (bill_id,)).fetchall()
    return {r["product_id"]: r["q"] for r in rows}


# ---------------------------------------------------------------------------
# Bill maths (the server is the source of truth; the browser mirrors it)
# ---------------------------------------------------------------------------
def compute_bill(data, user, conn):
    customer = (data.get("customerName") or "").strip()
    if not customer:
        raise ValidationError("Customer name is required.")
    raw_items = data.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise ValidationError("At least one item is required.")

    is_admin = user["role"] == "admin"
    items, wanted = [], {}
    subtotal, line_tax_sum = 0.0, 0.0
    for it in raw_items:
        if not isinstance(it, dict):
            raise ValidationError("Invalid item data.")
        pid, product = it.get("productId"), None
        if pid not in (None, ""):
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                raise ValidationError("Invalid product reference.")
            product = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if not product:
                raise ValidationError("A product on this bill was removed from the catalog.")
        else:
            pid = None

        if product is not None and not is_admin:
            # Cashiers can't override catalog prices: the catalog is authoritative.
            name, price = product["name"], product["price"]
            unit, gst = product["unit"] or "", product["gst_rate"]
        else:
            if product is None and not is_admin and not CASHIER_CUSTOM_ITEMS:
                raise ValidationError("Cashiers can only bill items from the product catalog.")
            name = (it.get("name") or "").strip()
            if not name:
                raise ValidationError("Every item needs a name.")
            price = num(it.get("price"), f"Price of '{name}'")
            unit = (it.get("unit") or "").strip()
            gst = num(it.get("gstRate"), f"GST of '{name}'")
            if gst > 100:
                raise ValidationError(f"GST of '{name}' is out of range.")
        qty = num(it.get("quantity"), f"Quantity of '{name}'", allow_zero=False)

        line = round(price * qty, 2)
        subtotal += line
        line_tax_sum += line * gst / 100
        if pid is not None:
            wanted[pid] = wanted.get(pid, 0.0) + qty
        items.append(dict(product_id=pid, name=name, price=price, unit=unit,
                          quantity=qty, gst_rate=gst, total=line))
    subtotal = round(subtotal, 2)

    d_type = data.get("discountType") if data.get("discountType") in ("percent", "flat") else "percent"
    d_value = num(data.get("discountValue"), "Discount")
    if d_type == "percent":
        if d_value > 100:
            raise ValidationError("Discount can't be more than 100%.")
        discount = round(subtotal * d_value / 100, 2)
    else:
        discount = round(d_value, 2)
    if discount > subtotal:
        raise ValidationError("Discount can't be more than the bill subtotal.")
    if not is_admin and subtotal and discount / subtotal * 100 > MAX_CASHIER_DISCOUNT + 1e-9:
        raise ValidationError(
            f"Cashiers can give at most {MAX_CASHIER_DISCOUNT:g}% discount. Ask an admin for more.")

    ratio = (subtotal - discount) / subtotal if subtotal else 0
    tax = round(line_tax_sum * ratio, 2)
    total = round(subtotal - discount + tax, 2)

    payment = data.get("paymentMethod")
    if payment not in PAYMENT_METHODS:
        payment = "Cash"
    return dict(customer=customer, items=items, wanted=wanted, subtotal=subtotal, d_type=d_type,
                d_value=d_value, discount=discount, tax=tax, total=total, payment=payment)


def bill_to_dict(b, items):
    return {
        "id": b["id"],
        "billNo": f"B-{b['id']:05d}",
        "customerName": b["customer_name"],
        "cashierName": b["cashier_name"] or "",
        "subtotal": b["subtotal"],
        "discountType": b["discount_type"],
        "discountValue": b["discount_value"],
        "discountAmount": b["discount_amount"],
        "tax": b["tax"],
        "total": b["total"],
        "paymentMethod": b["payment_method"],
        "createdAt": b["created_at"],
        "updatedAt": b["updated_at"],
        "items": [
            {"id": i["id"], "productId": i["product_id"], "name": i["item_name"],
             "price": i["price"], "unit": i["unit"], "quantity": i["quantity"],
             "gstRate": i["gst_rate"], "total": i["item_total"]}
            for i in items
        ],
    }


def fetch_bills(conn, clauses=(), params=()):
    """Bills + items in two queries (no N+1)."""
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    bills = conn.execute(
        f"SELECT * FROM bills {where} ORDER BY updated_at DESC, id DESC", params).fetchall()
    if not bills:
        return []
    ids = [b["id"] for b in bills]
    grouped = {i: [] for i in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT * FROM bill_items WHERE bill_id IN ({marks}) ORDER BY id", chunk):
            grouped[r["bill_id"]].append(r)
    return [bill_to_dict(b, grouped[b["id"]]) for b in bills]


def write_items_and_totals(conn, bill_id, c, now):
    conn.execute("DELETE FROM bill_items WHERE bill_id = ?", (bill_id,))
    conn.executemany(
        """INSERT INTO bill_items
           (bill_id, product_id, item_name, price, unit, quantity, gst_rate, item_total)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(bill_id, i["product_id"], i["name"], i["price"], i["unit"], i["quantity"],
          i["gst_rate"], i["total"]) for i in c["items"]])
    conn.execute(
        """UPDATE bills SET customer_name=?, subtotal=?, discount_type=?, discount_value=?,
           discount_amount=?, tax=?, total=?, payment_method=?, updated_at=? WHERE id=?""",
        (c["customer"], c["subtotal"], c["d_type"], c["d_value"], c["discount"],
         c["tax"], c["total"], c["payment"], now, bill_id))


# ---------------------------------------------------------------------------
# Frontend + security headers
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", gst_rates=GST_RATES, payment_methods=PAYMENT_METHODS)


@app.after_request
def add_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# API: bills
# ---------------------------------------------------------------------------
def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@app.route("/api/bills", methods=["GET"])
@login_required
def list_bills():
    user = current_user()
    q = (request.args.get("q") or "").strip()
    clauses, params = [], []
    if user["role"] != "admin":
        clauses.append("cashier_id = ?"); params.append(user["id"])  # cashiers see only their own
    if q:
        digits = q.upper().replace("B-", "").lstrip("0")
        clauses.append(
            "(customer_name LIKE ? ESCAPE '\\' OR cashier_name LIKE ? ESCAPE '\\' OR id = ? "
            "OR id IN (SELECT bill_id FROM bill_items WHERE item_name LIKE ? ESCAPE '\\'))")
        params += [_like(q), _like(q), int(digits) if digits.isdigit() else -1, _like(q)]
    conn = get_db()
    result = fetch_bills(conn, clauses, params)
    conn.close()
    return jsonify(result)


@app.route("/api/bills", methods=["POST"])
@login_required
def create_bill():
    user = current_user()
    try:
        with write_tx() as conn:
            c = compute_bill(body(), user, conn)
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO bills (customer_name, total, created_at, updated_at, cashier_id, cashier_name)
                   VALUES (?,?,?,?,?,?)""",
                (c["customer"], c["total"], now, now, user["id"], user["username"]))
            bill_id = cur.lastrowid
            write_items_and_totals(conn, bill_id, c, now)
            adjust_stock(conn, {}, c["wanted"], "sale", bill_id, user)
            out = fetch_bills(conn, ["id = ?"], (bill_id,))[0]
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(out), 201


@app.route("/api/bills/<int:bill_id>", methods=["GET"])
@login_required
def get_bill(bill_id):
    user = current_user()
    conn = get_db()
    found = fetch_bills(conn, ["id = ?"], (bill_id,))
    conn.close()
    if not found or (user["role"] != "admin" and found[0]["cashierName"] != user["username"]):
        return jsonify({"error": "Bill not found."}), 404
    return jsonify(found[0])


@app.route("/api/bills/<int:bill_id>", methods=["PUT"])
@admin_required
def update_bill(bill_id):
    user = current_user()
    try:
        with write_tx() as conn:
            if not conn.execute("SELECT 1 FROM bills WHERE id = ?", (bill_id,)).fetchone():
                raise NotFoundError()
            c = compute_bill(body(), user, conn)
            old = bill_stock_map(conn, bill_id)
            write_items_and_totals(conn, bill_id, c, now_iso())
            adjust_stock(conn, old, c["wanted"], "bill_edit", bill_id, user)
            out = fetch_bills(conn, ["id = ?"], (bill_id,))[0]
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    except NotFoundError:
        return jsonify({"error": "Bill not found."}), 404
    return jsonify(out)


@app.route("/api/bills/<int:bill_id>", methods=["DELETE"])
@admin_required
def delete_bill(bill_id):
    user = current_user()
    try:
        with write_tx() as conn:
            if not conn.execute("SELECT 1 FROM bills WHERE id = ?", (bill_id,)).fetchone():
                raise NotFoundError()
            adjust_stock(conn, bill_stock_map(conn, bill_id), {}, "bill_delete", bill_id, user)
            conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
    except NotFoundError:
        return jsonify({"error": "Bill not found."}), 404
    return jsonify({"deleted": True})


@app.route("/api/bills/export.csv")
@admin_required
def export_csv():
    conn = get_db()
    bills = fetch_bills(conn)
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Bill No", "Date (UTC)", "Customer", "Cashier", "Items", "Subtotal",
                "Discount", "GST", "Total", "Payment"])
    for b in bills:
        w.writerow([b["billNo"], b["createdAt"], b["customerName"], b["cashierName"],
                    len(b["items"]), b["subtotal"], b["discountAmount"], b["tax"],
                    b["total"], b["paymentMethod"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bills.csv"})


# ---------------------------------------------------------------------------
# API: products + stock
# ---------------------------------------------------------------------------
def parse_product(data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValidationError("Product name is required.")
    price = num(data.get("price"), "Price")
    gst = num(data.get("gstRate"), "GST")
    if gst > 100:
        raise ValidationError("GST is out of range.")
    sku = (data.get("sku") or "").strip() or None
    reorder = num(data.get("reorderLevel"), "Reorder level")
    return name, sku, price, (data.get("unit") or "").strip(), gst, reorder


def product_to_dict(r):
    stock = r["stock"]
    return {"id": r["id"], "name": r["name"], "sku": r["sku"], "price": r["price"],
            "unit": r["unit"], "gstRate": r["gst_rate"], "stock": stock,
            "reorderLevel": r["reorder_level"],
            "lowStock": stock is not None and stock <= r["reorder_level"]}


@app.route("/api/products", methods=["GET"])
@login_required
def list_products():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return jsonify([product_to_dict(r) for r in rows])


@app.route("/api/products", methods=["POST"])
@admin_required
def create_product():
    data = body()
    user = current_user()
    try:
        name, sku, price, unit, gst, reorder = parse_product(data)
        stock = None if data.get("stock") in (None, "") else num(data.get("stock"), "Opening stock")
        with write_tx() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO products (name, sku, price, unit, gst_rate, stock, reorder_level) "
                    "VALUES (?,?,?,?,?,?,?)", (name, sku, price, unit, gst, stock, reorder))
            except sqlite3.IntegrityError:
                return jsonify({"error": "That barcode/SKU already exists."}), 409
            if stock is not None:
                log_movement(conn, cur.lastrowid, stock, stock, "opening", None, user)
            row = conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(product_to_dict(row)), 201


@app.route("/api/products/<int:pid>", methods=["PUT"])
@admin_required
def update_product(pid):
    # Stock is deliberately NOT editable here: it only moves through the
    # ledger (sales or POST /stock), so every change is accounted for.
    try:
        name, sku, price, unit, gst, reorder = parse_product(body())
        with write_tx() as conn:
            try:
                cur = conn.execute(
                    "UPDATE products SET name=?, sku=?, price=?, unit=?, gst_rate=?, reorder_level=? "
                    "WHERE id=?", (name, sku, price, unit, gst, reorder, pid))
            except sqlite3.IntegrityError:
                return jsonify({"error": "That barcode/SKU already exists."}), 409
            if cur.rowcount == 0:
                raise NotFoundError()
            row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    except NotFoundError:
        return jsonify({"error": "Product not found."}), 404
    return jsonify(product_to_dict(row))


@app.route("/api/products/<int:pid>", methods=["DELETE"])
@admin_required
def delete_product(pid):
    with write_tx() as conn:
        cur = conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    if cur.rowcount == 0:
        return jsonify({"error": "Product not found."}), 404
    return jsonify({"deleted": True})


@app.route("/api/products/<int:pid>/stock", methods=["POST"])
@admin_required
def change_stock(pid):
    """Body: {"change": 20} to receive (+) / write off (-), or {"set": 47} for a
    physical count. Optional "note". Starting from untracked counts from 0."""
    data = body()
    user = current_user()
    note = (data.get("note") or "").strip()[:200] or None
    try:
        with write_tx() as conn:
            row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if not row:
                raise NotFoundError()
            current = row["stock"]
            if data.get("set") not in (None, ""):
                target = num(data["set"], "Stock count")
                change, reason = target - (current or 0), "count"
            elif data.get("change") not in (None, ""):
                change = num(data["change"], "Change", allow_negative=True)
                target = (current or 0) + change
                reason = "restock" if change > 0 else "adjustment"
                if target < -1e-9:
                    raise ValidationError("Stock can't go below zero.")
            else:
                raise ValidationError("Provide either 'change' or 'set'.")
            target, change = round(target, 3), round(change, 3)
            if change == 0 and current is not None:
                raise ValidationError("That doesn't change the stock.")
            conn.execute("UPDATE products SET stock = ? WHERE id = ?", (target, pid))
            log_movement(conn, pid, change, target, reason, None, user, note)
            out = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    except NotFoundError:
        return jsonify({"error": "Product not found."}), 404
    return jsonify(product_to_dict(out))


@app.route("/api/products/<int:pid>/movements")
@admin_required
def product_movements(pid):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM stock_movements WHERE product_id = ? ORDER BY id DESC LIMIT 50", (pid,)).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "change": r["change"], "balance": r["balance"],
                     "reason": r["reason"], "billId": r["bill_id"],
                     "billNo": f"B-{r['bill_id']:05d}" if r["bill_id"] else None,
                     "username": r["username"], "note": r["note"], "createdAt": r["created_at"]}
                    for r in rows])


# ---------------------------------------------------------------------------
# API: dashboard stats
# ---------------------------------------------------------------------------
@app.route("/api/stats")
@admin_required
def stats():
    try:
        offset = int(request.args.get("tzOffset", 0))  # minutes, like JS getTimezoneOffset()
    except ValueError:
        offset = 0
    local_now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=offset)
    today = local_now.date()
    shift = f"{-offset:+d} minutes"

    conn = get_db()
    day_rows = conn.execute(
        "SELECT date(created_at, ?) AS d, COUNT(*) AS n, SUM(total) AS s FROM bills "
        "WHERE date(created_at, ?) >= ? GROUP BY d",
        (shift, shift, str(today - timedelta(days=29)))).fetchall()
    by_day = {r["d"]: (r["n"], r["s"] or 0) for r in day_rows}
    month = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(total),0) s FROM bills "
        "WHERE strftime('%Y-%m', created_at, ?) = ?", (shift, today.strftime("%Y-%m"))).fetchone()
    overall = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) s FROM bills").fetchone()
    top = conn.execute(
        """SELECT item_name AS name, SUM(quantity) AS qty, SUM(item_total) AS revenue
           FROM bill_items GROUP BY LOWER(item_name) ORDER BY revenue DESC LIMIT 5""").fetchall()
    pay = conn.execute(
        "SELECT payment_method AS m, COUNT(*) AS n, SUM(total) AS s FROM bills GROUP BY m").fetchall()
    cashiers = conn.execute(
        """SELECT COALESCE(NULLIF(cashier_name,''), '(before logins)') AS name,
                  COUNT(*) AS n, SUM(total) AS s
           FROM bills GROUP BY name ORDER BY s DESC""").fetchall()
    low = conn.execute(
        """SELECT name, unit, stock, reorder_level FROM products
           WHERE stock IS NOT NULL AND stock <= reorder_level ORDER BY stock LIMIT 10""").fetchall()
    conn.close()

    last7 = []
    for k in range(6, -1, -1):
        d = today - timedelta(days=k)
        n, s = by_day.get(str(d), (0, 0))
        last7.append({"date": str(d), "label": d.strftime("%a"), "bills": n, "sales": round(s, 2)})
    t_n, t_s = by_day.get(str(today), (0, 0))
    return jsonify({
        "today": {"bills": t_n, "sales": round(t_s, 2)},
        "month": {"bills": month["n"], "sales": round(month["s"], 2)},
        "allTime": {"bills": overall["n"], "sales": round(overall["s"], 2),
                    "average": round(overall["s"] / overall["n"], 2) if overall["n"] else 0},
        "last7": last7,
        "topItems": [{"name": r["name"], "qty": r["qty"], "revenue": round(r["revenue"], 2)} for r in top],
        "payments": [{"method": r["m"], "bills": r["n"], "sales": round(r["s"], 2)} for r in pay],
        "cashiers": [{"name": r["name"], "bills": r["n"], "sales": round(r["s"], 2)} for r in cashiers],
        "lowStock": [{"name": r["name"], "unit": r["unit"], "stock": r["stock"],
                      "reorderLevel": r["reorder_level"]} for r in low],
    })


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=port)
