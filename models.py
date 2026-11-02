"""Database models for Makhzoon.

StoreInventory is the authoritative source for current stock. Product.quantity
is retained as a compatibility cache and every application stock write calls
``Product.sync_quantity`` in the same transaction.
"""

from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


user_store_assignments = db.Table(
    "user_store_assignments",
    db.Column(
        "user_id",
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "store_id",
        db.Integer,
        db.ForeignKey("stores.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


def utc_now():
    """Return naive UTC for the project's timestamp-without-time-zone columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model, UserMixin):
    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint("role IN ('owner', 'staff')", name="ck_users_role_valid"),
        db.CheckConstraint(
            "email IS NULL OR email = lower(trim(email))",
            name="ck_users_email_normalized",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="staff", index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    date_created = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_login_at = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime)

    assigned_stores = db.relationship(
        "Store",
        secondary=user_store_assignments,
        back_populates="assigned_users",
        order_by="Store.name",
    )
    reset_tokens = db.relationship(
        "PasswordResetToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    created_orders = db.relationship(
        "Order",
        foreign_keys="Order.created_by_user_id",
        back_populates="created_by_user",
    )
    status_changed_orders = db.relationship(
        "Order",
        foreign_keys="Order.status_changed_by_user_id",
        back_populates="status_changed_by_user",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.password_changed_at = utc_now()

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_owner(self):
        return self.role == "owner"

    @property
    def is_staff(self):
        return self.role == "staff"


class Store(db.Model):
    """A dynamically managed selling and stocking location."""

    __tablename__ = "stores"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    inventories = db.relationship(
        "StoreInventory", back_populates="store", cascade="all, delete-orphan"
    )
    orders = db.relationship("Order", back_populates="store")
    assigned_users = db.relationship(
        "User", secondary=user_store_assignments, back_populates="assigned_stores"
    )


class Product(db.Model):
    __tablename__ = "products"
    __table_args__ = (
        db.CheckConstraint("quantity >= 0", name="ck_products_quantity_nonnegative"),
        db.CheckConstraint("purchase_price >= 0", name="ck_products_purchase_price_nonnegative"),
        db.CheckConstraint("wholesale_price >= 0", name="ck_products_wholesale_price_nonnegative"),
        db.CheckConstraint("semi_wholesale_price >= 0", name="ck_products_semi_price_nonnegative"),
        db.CheckConstraint("retail_price >= 0", name="ck_products_retail_price_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    supplier = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    purchase_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    wholesale_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    semi_wholesale_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    retail_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    reorder_level = db.Column(db.Integer, nullable=False, default=5)
    date_added = db.Column(db.DateTime, default=utc_now, nullable=False)

    order_items = db.relationship("OrderItem", back_populates="product")
    store_inventories = db.relationship(
        "StoreInventory",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="StoreInventory.store_id",
    )
    history = db.relationship(
        "ProductHistory",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductHistory.created_at.desc()",
    )

    PRICE_FIELDS = {
        "wholesale": "wholesale_price",
        "semi_wholesale": "semi_wholesale_price",
        "retail": "retail_price",
    }

    def price_for_tier(self, tier):
        """Return the merchant-entered price for an explicitly selected tier."""
        field = self.PRICE_FIELDS.get(tier)
        if not field:
            raise ValueError("Unknown selling price tier")
        return getattr(self, field)

    def sync_quantity(self):
        """Synchronize the legacy total from authoritative per-store rows."""
        self.quantity = sum(inventory.quantity for inventory in self.store_inventories)
        return self.quantity

    def inventory_for(self, store_id):
        return next((row for row in self.store_inventories if row.store_id == store_id), None)


class StoreInventory(db.Model):
    __tablename__ = "store_inventory"
    __table_args__ = (
        db.UniqueConstraint("store_id", "product_id", name="uq_store_inventory"),
        db.CheckConstraint("quantity >= 0", name="ck_store_inventory_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("stores.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    store = db.relationship("Store", back_populates="inventories")
    product = db.relationship("Product", back_populates="store_inventories")


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('Pending', 'Processing', 'Completed', 'Cancelled')",
            name="ck_orders_status_valid",
        ),
        db.CheckConstraint(
            "submission_token_hash IS NULL OR length(submission_token_hash) = 64",
            name="ck_orders_submission_token_hash_length",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20))
    customer_email = db.Column(db.String(254))
    notes = db.Column(db.Text)
    store_id = db.Column(db.Integer, db.ForeignKey("stores.id"), nullable=True, index=True)
    employee_reference = db.Column(db.String(100), nullable=True)
    submission_token_hash = db.Column(db.CHAR(64), nullable=True, unique=True, index=True)
    created_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status_changed_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_date = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    status = db.Column(db.String(20), default="Pending", nullable=False, index=True)
    inventory_restored = db.Column(db.Boolean, default=False, nullable=False)

    store = db.relationship("Store", back_populates="orders")
    created_by_user = db.relationship(
        "User", foreign_keys=[created_by_user_id], back_populates="created_orders"
    )
    status_changed_by_user = db.relationship(
        "User", foreign_keys=[status_changed_by_user_id], back_populates="status_changed_orders"
    )
    items = db.relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )

    def get_total(self):
        return sum((item.subtotal for item in self.items), Decimal("0.00"))

    @property
    def total_quantity(self):
        return sum(item.quantity for item in self.items)


class OrderItem(db.Model):
    __tablename__ = "order_items"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        db.CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price_nonnegative"),
        db.CheckConstraint(
            "price_tier IS NULL OR price_tier IN ('wholesale', 'semi_wholesale', 'retail')",
            name="ck_order_items_price_tier_valid",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    price_tier = db.Column(db.String(24), nullable=True)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    purchase_unit_cost = db.Column(db.Numeric(12, 2), nullable=True)

    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product", back_populates="order_items")

    @property
    def subtotal(self):
        return self.quantity * self.unit_price

    @property
    def gross_profit(self):
        if self.purchase_unit_cost is None:
            return None
        return self.quantity * (self.unit_price - self.purchase_unit_cost)


class ProductHistory(db.Model):
    """Minimal audit facts; order lines remain authoritative for sales."""

    __tablename__ = "product_history"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey("stores.id"), nullable=True, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    quantity_change = db.Column(db.Integer, nullable=True)
    old_quantity = db.Column(db.Integer, nullable=True)
    new_quantity = db.Column(db.Integer, nullable=True)
    purchase_unit_cost = db.Column(db.Numeric(12, 2), nullable=True)
    price_field = db.Column(db.String(40), nullable=True)
    old_price = db.Column(db.Numeric(12, 2), nullable=True)
    new_price = db.Column(db.Numeric(12, 2), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    product = db.relationship("Product", back_populates="history")
    store = db.relationship("Store")
    order = db.relationship("Order")


class ReportDelivery(db.Model):
    """Durable idempotency record for scheduled report email attempts."""

    __tablename__ = "report_deliveries"
    __table_args__ = (
        db.UniqueConstraint("schedule_type", "period_start", "period_end", name="uq_report_delivery_period"),
    )

    id = db.Column(db.Integer, primary_key=True)
    schedule_type = db.Column(db.String(20), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    recipient = db.Column(db.String(254), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    attempts = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text)
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class PasswordResetToken(db.Model):
    """Single-use password reset token metadata; raw tokens are never stored."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        db.CheckConstraint(
            "length(token_hash) = 64", name="ck_password_reset_token_hash_length"
        ),
        db.CheckConstraint(
            "expires_at > created_at", name="ck_password_reset_token_expiry"
        ),
        db.CheckConstraint(
            "used_at IS NULL OR used_at >= created_at",
            name="ck_password_reset_token_use_time",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash = db.Column(db.CHAR(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="reset_tokens")


class LoginAttempt(db.Model):
    """Database-backed failed-login record used for multi-process throttling."""

    __tablename__ = "login_attempts"
    __table_args__ = (
        db.Index("ix_login_attempts_identity_time", "identity_hash", "attempted_at"),
        db.Index("ix_login_attempts_remote_time", "remote_addr_hash", "attempted_at"),
        db.CheckConstraint(
            "length(identity_hash) = 64", name="ck_login_attempt_identity_hash_length"
        ),
        db.CheckConstraint(
            "length(remote_addr_hash) = 64", name="ck_login_attempt_remote_hash_length"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    identity_hash = db.Column(db.CHAR(64), nullable=False)
    remote_addr_hash = db.Column(db.CHAR(64), nullable=False)
    attempted_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    was_successful = db.Column(db.Boolean, nullable=False, default=False)
