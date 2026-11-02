"""Makhzoon: multi-store retail inventory, orders, and reporting."""

import hashlib
import hmac
import io
import os
import re
import secrets
import smtplib
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps
from urllib.parse import urljoin, urlsplit

import click
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
    session, url_for,
)
from flask_login import (
    LoginManager, current_user, login_required, login_user, logout_user,
)
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import selectinload
from werkzeug.security import check_password_hash, generate_password_hash

from analytics import build_report
from config import Config
from models import (
    db, LoginAttempt, Order, OrderItem, PasswordResetToken, Product,
    ProductHistory, Store, StoreInventory, User, utc_now,
)
from reporting import generate_report_pdf
from time_utils import (
    local_date_range_to_utc, local_today as business_local_today, utc_to_local,
)

# Application initialization
app = Flask(__name__)
app.config.from_object(Config)
if not app.config.get("SECRET_KEY"):
    raise RuntimeError(
        "SECRET_KEY is not configured. Set it in the project-local .env file."
    )
if not app.config.get("SQLALCHEMY_DATABASE_URI"):
    raise RuntimeError(
        "DATABASE_URL is not configured. Create the project-local .env file "
        "from .env.example before starting Flask."
    )
if app.config["SQLALCHEMY_DATABASE_URI"].startswith(
    ("postgresql://", "postgresql+psycopg2://")
):
    engine_options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
    connect_args = dict(engine_options.get("connect_args") or {})
    connect_args.setdefault("options", "-c timezone=UTC")
    engine_options["connect_args"] = connect_args
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
db.init_app(app)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    if not user or not user.is_active:
        return None
    session_version = session.get("password_changed_at")
    current_version = (
        user.password_changed_at.isoformat() if user.password_changed_at else None
    )
    return user if session_version == current_version else None


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication_required"}), 401
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

# Configuration and request protection
@app.context_processor
def template_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {
        "csrf_token": lambda: session["csrf_token"],
        "price_tier_labels": PRICE_TIER_LABELS,
    }


@app.template_filter("local_datetime")
def local_datetime_filter(value, format_string="%d %b %Y, %H:%M"):
    local_value = utc_to_local(value, app.config["BUSINESS_TIMEZONE"])
    return local_value.strftime(format_string) if local_value else ""


@app.before_request
def protect_state_changes():
    """Dependency-free session-token CSRF guard for every state-changing form."""
    if request.method == "POST" and app.config.get("CSRF_ENABLED", True):
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, description="Invalid or missing CSRF token.")


# Constants and shared helpers
MONEY_QUANTUM = Decimal("0.01")
PRICE_FIELDS = (
    "purchase_price",
    "wholesale_price",
    "semi_wholesale_price",
    "retail_price",
)
PRICE_TIER_LABELS = {
    "wholesale": "Wholesale",
    "semi_wholesale": "Semi-wholesale",
    "retail": "Retail",
}
ALLOWED_ORDER_STATUSES = ("Pending", "Processing", "Completed", "Cancelled")
ALLOWED_STATUS_TRANSITIONS = {
    "Pending": {"Processing", "Cancelled"},
    "Processing": {"Completed", "Cancelled"},
    "Completed": set(),
    "Cancelled": set(),
}
ORDER_STATUS_BADGES = {
    "Pending": "text-bg-warning",
    "Processing": "text-bg-info",
    "Completed": "text-bg-success",
    "Cancelled": "text-bg-secondary",
}
PRODUCT_SORT_OPTIONS = {
    "id": ("ID", Product.id),
    "name": ("Name", Product.name),
    "category": ("Category", Product.category),
    "retail_price": ("Retail Price", Product.retail_price),
    "quantity": ("Quantity", Product.quantity),
    "reorder_level": ("Reorder Level", Product.reorder_level),
}
STOCK_STATUS_OPTIONS = (
    ("in_stock", "In Stock"),
    ("low_stock", "Low Stock"),
    ("out_of_stock", "Out of Stock"),
)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def money(value):
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def local_today():
    return business_local_today(app.config["BUSINESS_TIMEZONE"])


def local_bounds(start_date, end_date_exclusive):
    return local_date_range_to_utc(
        start_date, end_date_exclusive, app.config["BUSINESS_TIMEZONE"]
    )


def owner_required(view):
    """Require an authenticated Owner and return 403 for authenticated Staff."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_owner:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def authorized_store_ids(user=None):
    user = user or current_user
    if user.is_owner:
        return None
    return {store.id for store in user.assigned_stores}


def can_access_store(store_id, user=None):
    allowed = authorized_store_ids(user)
    return allowed is None or store_id in allowed


def accessible_order_or_404(order_id, lock=False):
    query = Order.query.filter_by(id=order_id)
    if lock:
        query = query.with_for_update()
    order = query.first_or_404()
    if not current_user.is_owner and not can_access_store(order.store_id):
        abort(403)
    return order


def is_safe_redirect_target(target):
    if not target:
        return False
    host = urlsplit(request.host_url)
    destination = urlsplit(urljoin(request.host_url, target))
    return destination.scheme in {"http", "https"} and host.netloc == destination.netloc


def normalize_email(value):
    return (value or "").strip().lower()


def token_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def private_digest(value):
    key = str(app.config["SECRET_KEY"]).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def login_attempt_key(email):
    return private_digest(normalize_email(email))


def login_is_limited(identity_hash, remote_addr_hash):
    cutoff = utc_now() - timedelta(minutes=app.config["LOGIN_ATTEMPT_WINDOW_MINUTES"])
    LoginAttempt.query.filter(LoginAttempt.attempted_at < cutoff).delete(
        synchronize_session=False
    )
    failures = LoginAttempt.query.filter(
        LoginAttempt.was_successful.is_(False),
        LoginAttempt.attempted_at >= cutoff,
    )
    identity_failures = failures.filter(
        LoginAttempt.identity_hash == identity_hash
    ).count()
    address_failures = failures.filter(
        LoginAttempt.remote_addr_hash == remote_addr_hash
    ).count()
    maximum = app.config["LOGIN_MAX_ATTEMPTS"]
    return identity_failures >= maximum or address_failures >= maximum


def issue_password_reset(user):
    raw_token = secrets.token_urlsafe(32)
    now = utc_now()
    reset = PasswordResetToken(
        user=user,
        token_hash=token_digest(raw_token),
        created_at=now,
        expires_at=now + timedelta(minutes=app.config["PASSWORD_RESET_MINUTES"]),
    )
    db.session.add(reset)
    db.session.commit()
    return raw_token, reset


def send_password_reset_email(recipient, reset_url):
    """Send a reset link only when authentication email is explicitly enabled."""
    if not app.config.get("ENABLE_AUTH_EMAIL"):
        raise RuntimeError("Authentication email is disabled.")
    required = ("SMTP_HOST", "SMTP_FROM")
    if any(not app.config.get(key) for key in required):
        raise RuntimeError("Authentication email configuration is incomplete.")
    message = EmailMessage()
    message["From"] = app.config["SMTP_FROM"]
    message["To"] = recipient
    message["Subject"] = "Reset your Makhzoon password"
    message.set_content(
        "A password reset was requested for your Makhzoon account.\n\n"
        f"Open this single-use link to choose a new password:\n{reset_url}\n\n"
        f"This link expires in {app.config['PASSWORD_RESET_MINUTES']} minutes. "
        "If you did not request it, you can ignore this message."
    )
    with smtplib.SMTP(
        app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=30
    ) as smtp:
        if app.config.get("SMTP_USE_TLS"):
            smtp.starttls()
        if app.config.get("SMTP_USERNAME"):
            smtp.login(app.config["SMTP_USERNAME"], app.config["SMTP_PASSWORD"])
        smtp.send_message(message)


def create_and_send_reset(user):
    if not app.config.get("ENABLE_AUTH_EMAIL") or any(
        not app.config.get(key) for key in ("SMTP_HOST", "SMTP_FROM")
    ):
        raise RuntimeError("Authentication email is not fully configured.")
    base_url = app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    parsed_base = urlsplit(base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise RuntimeError("PUBLIC_BASE_URL is required for password-reset email.")
    raw_token, reset = issue_password_reset(user)
    reset_url = f"{base_url}{url_for('password_reset', token=raw_token)}"
    try:
        send_password_reset_email(user.email, reset_url)
    except (RuntimeError, OSError, smtplib.SMTPException):
        db.session.delete(reset)
        db.session.commit()
        raise


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password", "")
        identity_hash = login_attempt_key(email)
        remote_addr_hash = private_digest(request.remote_addr or "unknown")
        if login_is_limited(identity_hash, remote_addr_hash):
            db.session.commit()
            flash("Too many sign-in attempts. Try again later.", "danger")
            return render_template("auth/login.html", email=email), 429
        user = User.query.filter(func.lower(User.email) == email).first() if email else None
        if user and user.is_active:
            password_valid = user.check_password(password)
        else:
            check_password_hash(DUMMY_PASSWORD_HASH, password)
            password_valid = False
        if not password_valid:
            db.session.add(LoginAttempt(
                identity_hash=identity_hash,
                remote_addr_hash=remote_addr_hash,
                was_successful=False,
            ))
            db.session.commit()
            flash("Invalid email or password.", "danger")
            return render_template("auth/login.html", email=email), 401
        LoginAttempt.query.filter(or_(
            LoginAttempt.identity_hash == identity_hash,
            LoginAttempt.remote_addr_hash == remote_addr_hash,
        )).delete(synchronize_session=False)
        user.last_login_at = utc_now()
        db.session.commit()
        next_target = request.args.get("next")
        session.clear()
        login_user(user, remember=False, duration=app.config["PERMANENT_SESSION_LIFETIME"])
        session.permanent = True
        session["password_changed_at"] = (
            user.password_changed_at.isoformat() if user.password_changed_at else None
        )
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(next_target if is_safe_redirect_target(next_target) else url_for("home"))
    return render_template("auth/login.html", email="")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have signed out.", "success")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        user = User.query.filter(
            func.lower(User.email) == email, User.is_active.is_(True)
        ).first() if email else None
        if user:
            try:
                create_and_send_reset(user)
            except (RuntimeError, OSError, smtplib.SMTPException):
                app.logger.warning("A password reset email could not be delivered.")
        flash(
            "If an active account matches that email, a reset link has been sent.",
            "success",
        )
        return redirect(url_for("login"))
    return render_template("auth/forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def password_reset(token):
    digest = token_digest(token)
    candidate = PasswordResetToken.query.filter_by(token_hash=digest).first()
    now = utc_now()
    valid = bool(
        candidate
        and candidate.used_at is None
        and candidate.expires_at > now
        and candidate.user.is_active
    )
    if request.method == "GET":
        return render_template("auth/reset_password.html", token=token, valid=valid), (200 if valid else 400)
    if not candidate:
        abort(400, description="This password reset link is invalid or expired.")
    user = User.query.filter_by(id=candidate.user_id).with_for_update().first()
    reset = PasswordResetToken.query.filter_by(token_hash=digest).with_for_update().first()
    now = utc_now()
    if not user or not user.is_active or not reset or reset.used_at is not None or reset.expires_at <= now:
        db.session.rollback()
        abort(400, description="This password reset link is invalid or expired.")
    password = request.form.get("password", "")
    confirmation = request.form.get("password_confirmation", "")
    if len(password) < app.config["PASSWORD_MIN_LENGTH"]:
        flash(
            f"Password must be at least {app.config['PASSWORD_MIN_LENGTH']} characters.",
            "danger",
        )
        return render_template("auth/reset_password.html", token=token, valid=True), 400
    if password != confirmation:
        flash("Password confirmation does not match.", "danger")
        return render_template("auth/reset_password.html", token=token, valid=True), 400
    user.set_password(password)
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update(
        {PasswordResetToken.used_at: now}, synchronize_session=False
    )
    db.session.commit()
    session.clear()
    flash("Your password has been reset. Sign in with the new password.", "success")
    return redirect(url_for("login"))


@app.cli.command("create-owner")
@click.option("--email", prompt=True)
@click.option("--username", prompt=True)
def create_owner_command(email, username):
    """Create the first Owner without exposing a password on the command line."""
    email = normalize_email(email)
    username = username.strip()
    if User.query.filter_by(role="owner").first():
        raise click.ClickException(
            "An Owner already exists. Use reset-owner-password for recovery."
        )
    if not email or "@" not in email or len(email) > 254:
        raise click.ClickException("Enter a valid email address.")
    if not username or len(username) > 80:
        raise click.ClickException("Username must be 1 to 80 characters.")
    if User.query.filter(or_(func.lower(User.email) == email, User.username == username)).first():
        raise click.ClickException("A user with that email or username already exists.")
    password = click.prompt(
        "Password", hide_input=True, confirmation_prompt=True, type=str
    )
    if len(password) < app.config["PASSWORD_MIN_LENGTH"]:
        raise click.ClickException(
            f"Password must be at least {app.config['PASSWORD_MIN_LENGTH']} characters."
        )
    user = User(email=email, username=username, role="owner", is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo("Owner account created.")


@app.cli.command("reset-owner-password")
@click.option("--email", prompt=True)
def reset_owner_password_command(email):
    """Reset an Owner password as an offline administrative recovery path."""
    email = normalize_email(email)
    user = User.query.filter(
        func.lower(User.email) == email, User.role == "owner"
    ).first()
    if not user:
        raise click.ClickException("No Owner account matches that email.")
    password = click.prompt(
        "Password", hide_input=True, confirmation_prompt=True, type=str
    )
    if len(password) < app.config["PASSWORD_MIN_LENGTH"]:
        raise click.ClickException(
            f"Password must be at least {app.config['PASSWORD_MIN_LENGTH']} characters."
        )
    user.set_password(password)
    user.is_active = True
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update(
        {PasswordResetToken.used_at: utc_now()}, synchronize_session=False
    )
    db.session.commit()
    click.echo("Owner password reset.")


def get_product_stock_status(product):
    if product.quantity == 0:
        return {"key": "out_of_stock", "label": "Out of Stock", "badge_class": "text-bg-danger"}
    if product.quantity <= product.reorder_level:
        return {"key": "low_stock", "label": "Low Stock", "badge_class": "text-bg-warning"}
    return {"key": "in_stock", "label": "In Stock", "badge_class": "text-bg-success"}


def get_stock_status_condition(status):
    return {
        "in_stock": Product.quantity > Product.reorder_level,
        "low_stock": and_(Product.quantity > 0, Product.quantity <= Product.reorder_level),
        "out_of_stock": Product.quantity == 0,
    }.get(status)


def parse_nonnegative_int(value, label, errors, key):
    try:
        number = int(str(value).strip())
        if number < 0:
            raise ValueError
        return number
    except (TypeError, ValueError):
        errors[key] = f"{label} must be a whole number of 0 or greater."
        return None


def parse_nonnegative_money(value, label, errors, key, positive=False):
    try:
        amount = money(value)
        if amount < 0 or (positive and amount <= 0):
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError, TypeError):
        qualifier = "greater than 0" if positive else "0 or greater"
        errors[key] = f"{label} must be a valid amount of {qualifier}."
        return None


# Product validation and mutation helpers
def product_form_values(product=None, stores=None):
    stores = stores or []
    if product is None:
        values = {
            "name": "",
            "category": "",
            "supplier": "",
            "reorder_level": "5",
            "adjustment_type": "purchase",
        }
        values.update({field: "" for field in PRICE_FIELDS})
        values["inventories"] = {store.id: "0" for store in stores}
        return values
    values = {
        "name": product.name,
        "category": product.category,
        "supplier": product.supplier,
        "reorder_level": str(product.reorder_level),
        "adjustment_type": "correction",
        "inventories": {
            store.id: str(
                product.inventory_for(store.id).quantity
                if product.inventory_for(store.id)
                else 0
            )
            for store in stores
        },
    }
    values.update({field: str(getattr(product, field)) for field in PRICE_FIELDS})
    return values


def validate_product_form(form, stores):
    values = {
        "name": form.get("name", "").strip(),
        "category": form.get("category", "").strip(),
        "supplier": form.get("supplier", "").strip(),
        "reorder_level": form.get("reorder_level", "").strip(),
        "adjustment_type": form.get("adjustment_type", "correction").strip(),
        "inventories": {
            store.id: form.get(f"inventory_{store.id}", "0").strip()
            for store in stores
        },
    }
    values.update({field: form.get(field, "").strip() for field in PRICE_FIELDS})
    errors = {}
    for field, label in (("name", "Name"), ("category", "Category"), ("supplier", "Supplier")):
        if not values[field]:
            errors[field] = f"{label} is required."
        elif len(values[field]) > 100:
            errors[field] = f"{label} must be 100 characters or fewer."
    prices = {
        field: parse_nonnegative_money(
            values[field],
            field.replace("_", " ").title(),
            errors,
            field,
            positive=field != "purchase_price",
        )
        for field in PRICE_FIELDS
    }
    reorder = parse_nonnegative_int(values["reorder_level"], "Reorder level", errors, "reorder_level")
    inventories = {
        store.id: parse_nonnegative_int(
            values["inventories"][store.id],
            f"{store.name} quantity",
            errors,
            f"inventory_{store.id}",
        )
        for store in stores
    }
    if values["adjustment_type"] not in {"purchase", "correction"}:
        errors["adjustment_type"] = "Select a valid stock adjustment reason."
    cleaned = (
        None
        if errors
        else {
            "name": values["name"],
            "category": values["category"],
            "supplier": values["supplier"],
            "reorder_level": reorder,
            **prices,
            "inventories": inventories,
            "adjustment_type": values["adjustment_type"],
        }
    )
    return cleaned, values, errors


def apply_product_values(product, data, stores, is_new=False):
    """Apply catalog and stock changes while recording business-significant facts."""
    for field in ("name", "category", "supplier", "reorder_level"):
        setattr(product, field, data[field])
    for field in PRICE_FIELDS:
        old_value = None if is_new else money(getattr(product, field))
        new_value = data[field]
        setattr(product, field, new_value)
        if not is_new and old_value != new_value:
            db.session.add(ProductHistory(
                product=product, event_type="price_change", price_field=field,
                old_price=old_value, new_price=new_value,
                details=f"{field.replace('_', ' ').title()} updated.",
            ))
    # Keep the old price column as a retail-price compatibility alias.
    product.price = data["retail_price"]
    if is_new:
        db.session.add(ProductHistory(product=product, event_type="created", details="Product created."))
    for store in stores:
        inventory = product.inventory_for(store.id)
        if inventory is None:
            inventory = StoreInventory(product=product, store=store, quantity=0)
            db.session.add(inventory)
        old_quantity = inventory.quantity or 0
        new_quantity = data["inventories"][store.id]
        inventory.quantity = new_quantity
        delta = new_quantity - old_quantity
        if delta:
            is_purchase = delta > 0 and data["adjustment_type"] == "purchase"
            db.session.add(ProductHistory(
                product=product, store=store,
                event_type="restock" if is_purchase else "stock_correction",
                quantity_change=delta, old_quantity=old_quantity, new_quantity=new_quantity,
                purchase_unit_cost=data["purchase_price"] if is_purchase else None,
                details="Purchased inventory." if is_purchase else "Manual stock correction.",
            ))
    product.sync_quantity()


# Order and inventory workflow helpers
def order_form_values(form=None):
    if form is None:
        return {
            "submission_token": secrets.token_urlsafe(32),
            "store_id": "",
            "customer_name": "",
            "customer_phone": "",
            "customer_email": "",
            "notes": "",
            "items": [{"product_id": "", "quantity": "1", "price_tier": "retail", "errors": {}}],
        }
    product_ids = form.getlist("product_id")
    quantities = form.getlist("quantity")
    tiers = form.getlist("price_tier")
    count = max(len(product_ids), len(quantities), len(tiers))
    items = []
    for index in range(count):
        items.append({
            "product_id": product_ids[index].strip() if index < len(product_ids) else "",
            "quantity": quantities[index].strip() if index < len(quantities) else "",
            "price_tier": tiers[index].strip() if index < len(tiers) else "",
            "errors": {},
        })
    return {
        "submission_token": form.get("submission_token", "").strip(),
        "store_id": form.get("store_id", "").strip(),
        "customer_name": form.get("customer_name", "").strip(),
        "customer_phone": form.get("customer_phone", "").strip(),
        "customer_email": form.get("customer_email", "").strip(),
        "notes": form.get("notes", "").strip(),
        "items": items or [{"product_id": "", "quantity": "", "price_tier": "retail", "errors": {}}],
    }


def validate_order_form(form):
    values, errors, normalized, seen = order_form_values(form), {}, [], set()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", values["submission_token"]):
        errors["submission_token"] = "This order form has expired. Refresh it and try again."
    try:
        store_id = int(values["store_id"])
        if store_id <= 0:
            raise ValueError
    except ValueError:
        store_id = None
        errors["store_id"] = "Select a valid selling store."
    if not values["customer_name"]:
        errors["customer_name"] = "Customer Name is required."
    elif len(values["customer_name"]) > 100:
        errors["customer_name"] = "Customer Name must be 100 characters or fewer."
    if len(values["customer_email"]) > 254 or (
        values["customer_email"] and "@" not in values["customer_email"]
    ):
        errors["customer_email"] = "Enter a valid email address."
    for row in values["items"]:
        try:
            product_id = int(row["product_id"])
            if product_id <= 0:
                raise ValueError
        except ValueError:
            product_id = None
            row["errors"]["product_id"] = "Select a valid product."
        try:
            quantity = int(row["quantity"])
            if quantity <= 0:
                row["errors"]["quantity"] = "Quantity must be greater than 0."
        except ValueError:
            quantity = None
            row["errors"]["quantity"] = "Enter a positive whole-number quantity."
        if row["price_tier"] not in PRICE_TIER_LABELS:
            row["errors"]["price_tier"] = "Select a price tier."
        if product_id in seen:
            row["errors"]["product_id"] = "This product is already included."
        elif product_id:
            seen.add(product_id)
        if not row["errors"]:
            normalized.append(
                {"product_id": product_id, "quantity": quantity, "price_tier": row["price_tier"], "row": row}
            )
    if not normalized or any(row["errors"] for row in values["items"]):
        errors["items"] = "Add at least one valid order item."
    return values, store_id, normalized, errors


def lock_and_validate_order(store_id, items):
    store = (
        db.session.query(Store)
        .filter(Store.id == store_id, Store.is_active.is_(True))
        .with_for_update()
        .first()
    )
    if not store:
        return None, True
    product_ids = sorted(item["product_id"] for item in items)
    inventory_rows = (
        StoreInventory.query.filter(
            StoreInventory.store_id == store_id,
            StoreInventory.product_id.in_(product_ids),
        )
        .order_by(StoreInventory.product_id)
        .with_for_update()
        .all()
    )
    inventory_by_product = {row.product_id: row for row in inventory_rows}
    products = Product.query.filter(Product.id.in_(product_ids)).all()
    products_by_id = {product.id: product for product in products}
    has_errors = False
    for item in items:
        product = products_by_id.get(item["product_id"])
        inventory = inventory_by_product.get(item["product_id"])
        if not product:
            item["row"]["errors"]["product_id"] = "The selected product no longer exists."
            has_errors = True
            continue
        available = inventory.quantity if inventory else 0
        if item["quantity"] > available:
            item["row"]["errors"]["quantity"] = f"Only {available} unit{'s are' if available != 1 else ' is'} available in {store.name}."
            has_errors = True
        unit_price = money(product.price_for_tier(item["price_tier"]))
        if unit_price <= 0:
            item["row"]["errors"]["price_tier"] = "The selected price tier is not configured."
            has_errors = True
        item.update(product=product, inventory=inventory, unit_price=unit_price)
    return store, has_errors


def cancel_locked_order(order):
    if order.status not in {"Pending", "Processing"}:
        return False, f"A {order.status} order cannot be cancelled."
    if order.inventory_restored:
        return False, "This order's inventory has already been restored."
    if order.store_id is None:
        return False, "Legacy order stock cannot be restored automatically because its original store is unknown."
    product_ids = sorted({item.product_id for item in order.items})
    rows = (
        StoreInventory.query.filter(StoreInventory.store_id == order.store_id, StoreInventory.product_id.in_(product_ids))
        .order_by(StoreInventory.product_id).with_for_update().all()
    )
    by_product = {row.product_id: row for row in rows}
    if len(by_product) != len(product_ids):
        raise SQLAlchemyError("Missing store inventory during cancellation.")
    for item in order.items:
        row = by_product[item.product_id]
        old = row.quantity
        row.quantity += item.quantity
        item.product.sync_quantity()
        db.session.add(ProductHistory(
            product_id=item.product_id, store_id=order.store_id, order=order,
            event_type="sale_cancelled", quantity_change=item.quantity,
            old_quantity=old, new_quantity=row.quantity,
            details=f"Stock restored for cancelled order #{order.id}.",
        ))
    order.status, order.inventory_restored, order.updated_at = "Cancelled", True, utc_now()
    return True, None


# Dashboard routes
@app.route("/")
@login_required
def home():
    return redirect(url_for("dashboard" if current_user.is_owner else "orders"))


@app.route("/dashboard")
@owner_required
def dashboard():
    today = local_today()
    tomorrow_date = today + timedelta(days=1)
    month_start, tomorrow = local_bounds(today.replace(day=1), tomorrow_date)
    five_day_start, _ = local_bounds(today - timedelta(days=4), tomorrow_date)
    yesterday_start, yesterday_end = local_bounds(today - timedelta(days=1), today)
    sales_base = db.session.query(OrderItem).join(Order).filter(Order.status != "Cancelled")
    total_sales = sales_base.with_entities(func.coalesce(func.sum(OrderItem.quantity * OrderItem.unit_price), 0)).scalar()
    monthly_sales = sales_base.filter(Order.order_date >= month_start, Order.order_date < tomorrow).with_entities(func.coalesce(func.sum(OrderItem.quantity * OrderItem.unit_price), 0)).scalar()
    inventory_value = db.session.query(func.coalesce(func.sum(ProductHistory.quantity_change * ProductHistory.purchase_unit_cost), 0)).filter(ProductHistory.event_type == "restock", ProductHistory.quantity_change > 0, ProductHistory.purchase_unit_cost.isnot(None)).scalar()
    rankings = (
        db.session.query(Product.id, Product.name, func.sum(OrderItem.quantity).label("units"))
        .join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status != "Cancelled", Order.order_date >= five_day_start, Order.order_date < tomorrow)
        .group_by(Product.id, Product.name).all()
    )
    yesterday_sales = (
        db.session.query(OrderItem, Order, Product, Store).select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .outerjoin(Store, Store.id == Order.store_id)
        .filter(Order.status != "Cancelled", Order.order_date >= yesterday_start, Order.order_date < yesterday_end)
        .order_by(Order.order_date.desc()).limit(10).all()
    )
    return render_template(
        "dashboard.html", total_products=Product.query.count(), total_orders=Order.query.count(),
        low_stock_products=Product.query.filter(Product.quantity <= Product.reorder_level).count(),
        inventory_value=inventory_value or 0, total_sales=total_sales or 0, monthly_sales=monthly_sales or 0,
        top_products=sorted(rankings, key=lambda row: (-row.units, row.name))[:5],
        bottom_products=sorted(rankings, key=lambda row: (row.units, row.name))[:5],
        yesterday_sales=yesterday_sales,
    )


# Product and store routes
@app.route("/stores/add", methods=["POST"])
@owner_required
def add_store():
    name = request.form.get("name", "").strip()
    if not name or len(name) > 100:
        flash("Store name is required and must be 100 characters or fewer.", "danger")
    elif Store.query.filter(func.lower(Store.name) == name.lower()).first():
        flash("A store with that name already exists.", "warning")
    else:
        try:
            store = Store(name=name)
            db.session.add(store)
            db.session.flush()
            for product in Product.query.all():
                db.session.add(StoreInventory(store=store, product=product, quantity=0))
            db.session.commit()
            flash(f'"{name}" was added.', "success")
        except SQLAlchemyError:
            db.session.rollback()
            flash("The store could not be added.", "danger")
    return redirect(request.referrer or url_for("products"))


@app.route("/products")
@owner_required
def products():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    supplier = request.args.get("supplier", "").strip()
    stock_status = request.args.get("stock_status", "").strip()
    store_id = request.args.get("store_id", "").strip()
    sort = request.args.get("sort", "id") if request.args.get("sort", "id") in PRODUCT_SORT_OPTIONS else "id"
    direction = request.args.get("direction", "asc") if request.args.get("direction", "asc") in {"asc", "desc"} else "asc"
    query = Product.query.options(selectinload(Product.store_inventories).selectinload(StoreInventory.store))
    if search:
        term = f"%{search}%"
        query = query.filter(or_(Product.name.ilike(term), Product.category.ilike(term), Product.supplier.ilike(term)))
    if category:
        query = query.filter(Product.category == category)
    if supplier:
        query = query.filter(Product.supplier == supplier)
    condition = get_stock_status_condition(stock_status)
    if condition is not None:
        query = query.filter(condition)
    if store_id.isdigit():
        query = query.join(StoreInventory).filter(StoreInventory.store_id == int(store_id))
    column = PRODUCT_SORT_OPTIONS[sort][1]
    product_rows = query.order_by(column.desc() if direction == "desc" else column.asc(), Product.id.asc()).all()
    stores = Store.query.order_by(Store.name).all()
    return render_template(
        "products/list.html", products=product_rows, stores=stores,
        categories=[row[0] for row in db.session.query(Product.category).distinct().order_by(Product.category)],
        suppliers=[row[0] for row in db.session.query(Product.supplier).distinct().order_by(Product.supplier)],
        stock_statuses={p.id: get_product_stock_status(p) for p in product_rows}, stock_status_options=STOCK_STATUS_OPTIONS,
        sort_options=PRODUCT_SORT_OPTIONS, search=search, selected_category=category, selected_supplier=supplier,
        selected_stock_status=stock_status, selected_store_id=store_id, selected_sort=sort, selected_direction=direction,
        has_active_filters=bool(search or category or supplier or stock_status or store_id),
    )


@app.route("/products/add", methods=["GET", "POST"])
@owner_required
def add_product():
    stores = Store.query.filter_by(is_active=True).order_by(Store.name).all()
    values, errors = product_form_values(stores=stores), {}
    if request.method == "POST":
        data, values, errors = validate_product_form(request.form, stores)
        if data:
            try:
                product = Product(quantity=0, price=data["retail_price"])
                db.session.add(product)
                apply_product_values(product, data, stores, is_new=True)
                db.session.commit()
                flash(f'"{product.name}" was added successfully.', "success")
                return redirect(url_for("product_details", product_id=product.id))
            except SQLAlchemyError:
                db.session.rollback()
                flash("The product could not be saved.", "danger")
    return render_template("products/add.html", form_values=values, errors=errors, stores=stores)


@app.route("/products/<int:product_id>")
@owner_required
def product_details(product_id):
    product = db.get_or_404(Product, product_id)
    return render_template("products/details.html", product=product, stock_status=get_product_stock_status(product))


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@owner_required
def edit_product(product_id):
    product = db.get_or_404(Product, product_id)
    stores = Store.query.filter_by(is_active=True).order_by(Store.name).all()
    values, errors = product_form_values(product, stores), {}
    if request.method == "POST":
        data, values, errors = validate_product_form(request.form, stores)
        if data:
            try:
                apply_product_values(product, data, stores)
                db.session.commit()
                flash(f'"{product.name}" was updated successfully.', "success")
                return redirect(url_for("product_details", product_id=product.id))
            except SQLAlchemyError:
                db.session.rollback()
                flash("The product changes could not be saved.", "danger")
    return render_template("products/edit.html", product=product, stores=stores, form_values=values, errors=errors)


@app.route("/products/<int:product_id>/delete", methods=["POST"])
@owner_required
def delete_product(product_id):
    product = db.get_or_404(Product, product_id)
    if product.order_items:
        flash("This product cannot be deleted because it is used in an existing order.", "warning")
    else:
        try:
            name = product.name
            db.session.delete(product)
            db.session.commit()
            flash(f'"{name}" was deleted.', "success")
        except SQLAlchemyError:
            db.session.rollback()
            flash("The product could not be deleted.", "danger")
    return redirect(url_for("products"))


# Staff account management
def selected_store_ids(form):
    selected = set()
    for raw in form.getlist("store_ids"):
        try:
            selected.add(int(raw))
        except (TypeError, ValueError):
            continue
    return selected


def validate_staff_identity(email, username, current_user_id=None):
    errors = {}
    if not email or "@" not in email or len(email) > 254:
        errors["email"] = "Enter a valid email address."
    elif User.query.filter(
        func.lower(User.email) == email,
        User.id != current_user_id if current_user_id else User.id > 0,
    ).first():
        errors["email"] = "That email address is already in use."
    if not username or len(username) > 80:
        errors["username"] = "Username must be 1 to 80 characters."
    elif User.query.filter(
        User.username == username,
        User.id != current_user_id if current_user_id else User.id > 0,
    ).first():
        errors["username"] = "That username is already in use."
    return errors


@app.route("/users")
@owner_required
def users():
    staff = User.query.filter_by(role="staff").order_by(User.username, User.id).all()
    return render_template("users/list.html", staff_users=staff)


@app.route("/users/add", methods=["GET", "POST"])
@owner_required
def add_user():
    stores = Store.query.filter_by(is_active=True).order_by(Store.name).all()
    values = {"email": "", "username": "", "store_ids": set()}
    errors = {}
    if request.method == "POST":
        values = {
            "email": normalize_email(request.form.get("email")),
            "username": request.form.get("username", "").strip(),
            "store_ids": selected_store_ids(request.form),
        }
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        errors = validate_staff_identity(values["email"], values["username"])
        valid_store_ids = {store.id for store in stores}
        if not values["store_ids"] or not values["store_ids"].issubset(valid_store_ids):
            errors["store_ids"] = "Assign at least one active store."
        if len(password) < app.config["PASSWORD_MIN_LENGTH"]:
            errors["password"] = (
                f"Password must be at least {app.config['PASSWORD_MIN_LENGTH']} characters."
            )
        elif password != confirmation:
            errors["password_confirmation"] = "Password confirmation does not match."
        if not errors:
            try:
                user = User(
                    email=values["email"], username=values["username"],
                    role="staff", is_active=True,
                )
                user.set_password(password)
                user.assigned_stores = [
                    store for store in stores if store.id in values["store_ids"]
                ]
                db.session.add(user)
                db.session.commit()
                flash(f'Staff account "{user.username}" was created.', "success")
                return redirect(url_for("users"))
            except IntegrityError:
                db.session.rollback()
                errors["email"] = "The email or username is already in use."
    return render_template("users/form.html", staff_user=None, stores=stores, values=values, errors=errors)


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@owner_required
def edit_user(user_id):
    user = User.query.filter_by(id=user_id, role="staff").first_or_404()
    stores = Store.query.filter_by(is_active=True).order_by(Store.name).all()
    values = {
        "email": user.email or "",
        "username": user.username,
        "store_ids": {store.id for store in user.assigned_stores},
    }
    errors = {}
    if request.method == "POST":
        values = {
            "email": normalize_email(request.form.get("email")),
            "username": request.form.get("username", "").strip(),
            "store_ids": selected_store_ids(request.form),
        }
        errors = validate_staff_identity(values["email"], values["username"], user.id)
        valid_store_ids = {store.id for store in stores}
        if not values["store_ids"] or not values["store_ids"].issubset(valid_store_ids):
            errors["store_ids"] = "Assign at least one active store."
        if not errors:
            try:
                user.email = values["email"]
                user.username = values["username"]
                user.assigned_stores = [
                    store for store in stores if store.id in values["store_ids"]
                ]
                db.session.commit()
                flash(f'Staff account "{user.username}" was updated.', "success")
                return redirect(url_for("users"))
            except IntegrityError:
                db.session.rollback()
                errors["email"] = "The email or username is already in use."
    return render_template("users/form.html", staff_user=user, stores=stores, values=values, errors=errors)


@app.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@owner_required
def toggle_user_active(user_id):
    user = User.query.filter_by(id=user_id, role="staff").first_or_404()
    user.is_active = not user.is_active
    db.session.commit()
    flash(
        f'{user.username} is now {"active" if user.is_active else "inactive"}.',
        "success",
    )
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/reset-password", methods=["POST"])
@owner_required
def send_user_reset(user_id):
    user = User.query.filter_by(id=user_id, role="staff").first_or_404()
    if not user.is_active or not user.email:
        flash("Only an active Staff account with an email can receive a reset link.", "warning")
    else:
        try:
            create_and_send_reset(user)
            flash("A single-use password reset link was sent.", "success")
        except (RuntimeError, OSError, smtplib.SMTPException):
            app.logger.warning("A Staff password reset email could not be delivered.")
            flash("The reset email could not be delivered. Check the email configuration.", "danger")
    return redirect(url_for("users"))


# Order routes
@app.route("/orders")
@login_required
def orders():
    search, status = request.args.get("search", "").strip(), request.args.get("status", "").strip()
    date_from_raw, date_to_raw = request.args.get("date_from", ""), request.args.get("date_to", "")
    sort = request.args.get("sort", "order_date")
    direction = request.args.get("direction", "desc")
    query = Order.query.options(selectinload(Order.items), selectinload(Order.store))
    if not current_user.is_owner:
        query = query.filter(Order.store_id.in_(authorized_store_ids()))
    if search:
        conditions = [Order.customer_name.ilike(f"%{search}%")]
        if search.isdigit(): conditions.append(Order.id == int(search))
        query = query.filter(or_(*conditions))
    if status in ALLOWED_ORDER_STATUSES: query = query.filter(Order.status == status)
    try:
        date_from = date.fromisoformat(date_from_raw) if date_from_raw else None
    except ValueError:
        date_from = None
    try:
        date_to = date.fromisoformat(date_to_raw) if date_to_raw else None
    except ValueError:
        date_to = None
    if date_from:
        date_from_utc, _ = local_bounds(date_from, date_from + timedelta(days=1))
        query = query.filter(Order.order_date >= date_from_utc)
    if date_to:
        _, date_to_utc = local_bounds(date_to, date_to + timedelta(days=1))
        query = query.filter(Order.order_date < date_to_utc)
    order_rows = query.all()
    sort_keys = {
        "id": lambda order: order.id,
        "customer_name": lambda order: order.customer_name.lower(),
        "order_date": lambda order: order.order_date,
        "status": lambda order: order.status,
        "item_count": lambda order: len(order.items),
        "total_quantity": lambda order: order.total_quantity,
        "grand_total": lambda order: order.get_total(),
    }
    sort = sort if sort in sort_keys else "order_date"
    direction = direction if direction in {"asc", "desc"} else "desc"
    order_rows.sort(key=sort_keys[sort], reverse=direction == "desc")
    return render_template("orders/list.html", orders=order_rows, search=search, order_statuses=ALLOWED_ORDER_STATUSES,
                           status_badges=ORDER_STATUS_BADGES, selected_status=status if status in ALLOWED_ORDER_STATUSES else "",
                           has_active_filters=bool(search or status or date_from or date_to), sort_options={}, selected_sort=sort, selected_direction=direction,
                           selected_date_from=date_from_raw if date_from else "", selected_date_to=date_to_raw if date_to else "")


@app.route("/orders/add", methods=["GET", "POST"])
@login_required
def add_order():
    values, errors = order_form_values(), {}
    if request.method == "POST":
        values, store_id, items, errors = validate_order_form(request.form)
        submission_hash = (
            token_digest(values["submission_token"])
            if "submission_token" not in errors
            else None
        )
        if store_id and not can_access_store(store_id):
            abort(403)
        existing = (
            Order.query.filter_by(submission_token_hash=submission_hash).first()
            if submission_hash
            else None
        )
        if existing:
            if current_user.is_owner or can_access_store(existing.store_id):
                flash(f"Order #{existing.id} was already created from this submission.", "info")
                return redirect(url_for("order_details", order_id=existing.id))
            abort(409)
        if store_id and not Store.query.filter_by(id=store_id, is_active=True).first():
            errors["store_id"] = "Select an active selling store."
        if not errors:
            try:
                store, stock_errors = lock_and_validate_order(store_id, items)
                if stock_errors:
                    # A concurrent request with the same token may have
                    # committed while this request waited for the inventory
                    # locks. Prefer the completed idempotent result over a
                    # now-stale stock validation error.
                    existing = Order.query.filter_by(
                        submission_token_hash=submission_hash
                    ).first()
                    if existing:
                        db.session.rollback()
                        if current_user.is_owner or can_access_store(existing.store_id):
                            flash(f"Order #{existing.id} was already created from this submission.", "info")
                            return redirect(url_for("order_details", order_id=existing.id))
                        abort(409)
                    errors["items"] = "Correct the highlighted stock errors."
                    db.session.rollback()
                else:
                    # Recheck after the store/inventory locks. Duplicates for
                    # one store serialize here; the unique constraint remains
                    # the final guard for submissions spanning different locks.
                    existing = Order.query.filter_by(
                        submission_token_hash=submission_hash
                    ).first()
                    if existing:
                        db.session.rollback()
                        if current_user.is_owner or can_access_store(existing.store_id):
                            flash(f"Order #{existing.id} was already created from this submission.", "info")
                            return redirect(url_for("order_details", order_id=existing.id))
                        abort(409)
                    order = Order(
                        store=store,
                        customer_name=values["customer_name"],
                        customer_phone=values["customer_phone"] or None,
                        customer_email=values["customer_email"] or None,
                        notes=values["notes"] or None,
                        submission_token_hash=submission_hash,
                        created_by_user=current_user,
                        status_changed_by_user=current_user,
                    )
                    db.session.add(order)
                    db.session.flush()
                    for item in items:
                        old = item["inventory"].quantity
                        item["inventory"].quantity -= item["quantity"]
                        item["product"].sync_quantity()
                        order_item = OrderItem(
                            order=order, product=item["product"], quantity=item["quantity"], price_tier=item["price_tier"],
                            unit_price=item["unit_price"], purchase_unit_cost=item["product"].purchase_price or None,
                        )
                        db.session.add(order_item)
                        db.session.add(ProductHistory(
                            product=item["product"], store=store, order=order, event_type="sale",
                            quantity_change=-item["quantity"], old_quantity=old, new_quantity=item["inventory"].quantity,
                            new_price=item["unit_price"], price_field=item["price_tier"],
                            details=f"Sold through order #{order.id}.",
                        ))
                    db.session.commit()
                    flash(f"Order #{order.id} was created successfully.", "success")
                    return redirect(url_for("order_details", order_id=order.id))
            except IntegrityError:
                db.session.rollback()
                existing = Order.query.filter_by(
                    submission_token_hash=submission_hash
                ).first() if submission_hash else None
                if existing and (current_user.is_owner or can_access_store(existing.store_id)):
                    flash(f"Order #{existing.id} was already created from this submission.", "info")
                    return redirect(url_for("order_details", order_id=existing.id))
                flash("The order could not be created. No stock was changed.", "danger")
            except SQLAlchemyError:
                db.session.rollback()
                flash("The order could not be created. No stock was changed.", "danger")
    products = Product.query.order_by(Product.name, Product.id).all()
    stores_query = Store.query.filter_by(is_active=True)
    if not current_user.is_owner:
        stores_query = stores_query.filter(Store.id.in_(authorized_store_ids()))
    stores = stores_query.order_by(Store.name).all()
    store_ids = [store.id for store in stores]
    inventory_rows = StoreInventory.query.filter(
        StoreInventory.store_id.in_(store_ids)
    ).all() if store_ids else []
    inventory_map = {
        f"{row.store_id}:{row.product_id}": row.quantity for row in inventory_rows
    }
    available_product_ids = {
        row.product_id for row in inventory_rows if row.quantity > 0
    }
    return render_template(
        "orders/add.html", products=products, stores=stores,
        inventory_map=inventory_map, available_product_ids=available_product_ids,
        form_values=values, errors=errors,
    )


@app.route("/orders/<int:order_id>")
@login_required
def order_details(order_id):
    order = accessible_order_or_404(order_id)
    transitions = ALLOWED_STATUS_TRANSITIONS.get(order.status, set())
    if not current_user.is_owner:
        transitions = transitions.intersection({"Processing", "Completed"})
    return render_template("orders/details.html", order=order, status_badge=ORDER_STATUS_BADGES.get(order.status, "text-bg-secondary"), available_transitions=transitions)


@app.route("/orders/<int:order_id>/status", methods=["POST"])
@login_required
def update_order_status(order_id):
    requested = request.form.get("status", "").strip()
    try:
        order = accessible_order_or_404(order_id, lock=True)
        allowed = ALLOWED_STATUS_TRANSITIONS.get(order.status, set())
        if not current_user.is_owner:
            allowed = allowed.intersection({"Processing", "Completed"})
        if requested not in allowed:
            db.session.rollback()
            flash(f"Order #{order.id} cannot move from {order.status} to {requested}.", "warning")
            return redirect(url_for("order_details", order_id=order.id))
        if requested == "Cancelled":
            changed, message = cancel_locked_order(order)
            if not changed:
                db.session.rollback(); flash(message, "warning")
                return redirect(url_for("order_details", order_id=order.id))
        else:
            order.status, order.updated_at = requested, utc_now()
        order.status_changed_by_user = current_user
        db.session.commit()
        flash(f"Order #{order.id} is now {order.status}.", "success")
    except SQLAlchemyError:
        db.session.rollback(); flash("The order status could not be updated.", "danger")
    return redirect(url_for("order_details", order_id=order_id))


@app.route("/orders/<int:order_id>/cancel", methods=["POST"])
@owner_required
def cancel_order(order_id):
    try:
        order = accessible_order_or_404(order_id, lock=True)
        changed, message = cancel_locked_order(order)
        if not changed:
            db.session.rollback(); flash(message, "warning")
        else:
            order.status_changed_by_user = current_user
            db.session.commit(); flash(f"Order #{order.id} was cancelled and inventory restored.", "success")
    except SQLAlchemyError:
        db.session.rollback(); flash("The order could not be cancelled. Inventory was not changed.", "danger")
    return redirect(url_for("order_details", order_id=order_id))


# Inventory routes
@app.route("/inventory")
@owner_required
def inventory():
    search, store_id = request.args.get("search", "").strip(), request.args.get("store_id", "").strip()
    query = Product.query.options(selectinload(Product.store_inventories).selectinload(StoreInventory.store))
    if search:
        query = query.filter(Product.name.ilike(f"%{search}%"))
    if store_id.isdigit():
        query = query.join(StoreInventory).filter(StoreInventory.store_id == int(store_id))
    rows = query.order_by(Product.name).all()
    known_cost = db.session.query(func.coalesce(func.sum(ProductHistory.quantity_change * ProductHistory.purchase_unit_cost), 0)).filter(ProductHistory.event_type == "restock", ProductHistory.quantity_change > 0).scalar()
    return render_template("inventory/list.html", products=rows, stores=Store.query.order_by(Store.name).all(), search=search,
                           selected_store_id=store_id, stock_statuses={p.id: get_product_stock_status(p) for p in rows}, known_purchase_cost=known_cost or 0)


@app.route("/inventory/<int:product_id>/history")
@owner_required
def product_history(product_id):
    product = db.get_or_404(Product, product_id)
    return render_template("inventory/history.html", product=product, events=ProductHistory.query.filter_by(product_id=product_id).order_by(ProductHistory.created_at.desc(), ProductHistory.id.desc()).all())


# Reporting routes
def report_period():
    today = local_today()
    preset = request.args.get("period", "five_days")
    if preset == "current_month": start_date, end_date = today.replace(day=1), today + timedelta(days=1)
    elif preset == "previous_month":
        current_start = today.replace(day=1)
        previous_end = current_start
        start_date, end_date = (previous_end - timedelta(days=1)).replace(day=1), previous_end
    elif preset == "custom":
        try:
            start_date = date.fromisoformat(request.args.get("start", ""))
            selected_end = date.fromisoformat(request.args.get("end", ""))
            if selected_end < start_date: raise ValueError
            end_date = selected_end + timedelta(days=1)
        except ValueError:
            flash("Invalid custom report dates; showing the last five days.", "warning")
            preset, start_date, end_date = "five_days", today - timedelta(days=4), today + timedelta(days=1)
    else:
        preset, start_date, end_date = "five_days", today - timedelta(days=4), today + timedelta(days=1)
    start_utc, end_utc = local_bounds(start_date, end_date)
    return preset, start_date, end_date, start_utc, end_utc


@app.route("/reports")
@owner_required
def reports():
    preset, start_date, end_date, start, end = report_period()
    report = build_report(
        start, end, app.config["BUSINESS_TIMEZONE"], start_date, end_date
    )
    return render_template("reports/index.html", report=report, selected_period=preset,
                           selected_start=request.args.get("start", ""), selected_end=request.args.get("end", ""))


@app.route("/reports/pdf")
@owner_required
def report_pdf():
    preset, start_date, end_date, start, end = report_period()
    report = build_report(
        start, end, app.config["BUSINESS_TIMEZONE"], start_date, end_date
    )
    pdf = generate_report_pdf(report)
    filename = f"makhzoon-report-{preset}-{start_date:%Y%m%d}-{(end_date - timedelta(days=1)):%Y%m%d}.pdf"
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/api/store-inventory/<int:store_id>")
@login_required
def store_inventory_api(store_id):
    if not can_access_store(store_id):
        abort(403)
    return jsonify({str(row.product_id): row.quantity for row in StoreInventory.query.filter_by(store_id=store_id).all()})


# Application entry point
if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
