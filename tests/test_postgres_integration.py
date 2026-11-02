"""Opt-in PostgreSQL integration tests for Makhzoon.

Run this module in its own process; never include it in the normal SQLite test
run. Example (PowerShell), after provisioning a dedicated test database::

    $env:RUN_POSTGRES_INTEGRATION = "1"
    $env:TEST_DATABASE_URL = "postgresql+psycopg2://.../makhzoon_test"
    python -m unittest discover -s tests -p test_postgres_integration.py -v

The suite refuses destructive setup unless the URL is PostgreSQL, identifies a
database whose name contains ``test``, differs from the normal DATABASE_URL,
and the explicit opt-in flag is present. It creates and later drops only one
randomly named schema inside that dedicated database.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import importlib
import os
from pathlib import Path
import re
import secrets
import sys
from threading import Barrier
import unittest
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = PROJECT_ROOT / "migrations" / "000_baseline_post_004.sql"
MIGRATION_005_PATH = PROJECT_ROOT / "migrations" / "005_authentication_security.sql"
SCHEMA_PATTERN = re.compile(r"^makhzoon_it_[0-9a-f]{32}$")
POSTGRES_OPT_IN = os.getenv("RUN_POSTGRES_INTEGRATION") == "1"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def _configured_working_url():
    """Read the normal target only for identity comparison; never display it."""
    configured = os.getenv("DATABASE_URL", "").strip()
    if configured:
        return configured
    values = dotenv_values(PROJECT_ROOT / ".env")
    return (values.get("DATABASE_URL") or "").strip()


def _validated_test_url():
    """Fail closed before creating a schema or importing the application."""
    if not POSTGRES_OPT_IN:
        raise RuntimeError("Set RUN_POSTGRES_INTEGRATION=1 to opt in explicitly.")
    if not TEST_DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL is required.")

    test_url = make_url(TEST_DATABASE_URL)
    if test_url.get_backend_name() != "postgresql":
        raise RuntimeError("TEST_DATABASE_URL must use PostgreSQL.")
    if not test_url.database or "test" not in test_url.database.lower():
        raise RuntimeError("The dedicated database name must clearly contain 'test'.")

    working_raw = _configured_working_url()
    if not working_raw:
        raise RuntimeError(
            "DATABASE_URL must be configured so the test target can be proven distinct."
        )
    working_url = make_url(working_raw)
    if test_url == working_url:
        raise RuntimeError("TEST_DATABASE_URL must differ from DATABASE_URL.")
    if (test_url.database or "").lower() == (working_url.database or "").lower():
        raise RuntimeError(
            "The dedicated test database must have a different database name."
        )

    # A different password or driver is not a different database. Refuse the
    # same server/database identity even when the rendered URLs differ.
    test_identity = (
        test_url.host or "localhost",
        test_url.port or 5432,
        test_url.database,
    )
    working_identity = (
        working_url.host or "localhost",
        working_url.port or 5432,
        working_url.database,
    )
    if test_identity == working_identity:
        raise RuntimeError("The test target resolves to the working database.")
    return test_url


def _quote_schema(schema_name):
    if not SCHEMA_PATTERN.fullmatch(schema_name or ""):
        raise RuntimeError("Refusing an unverified integration-test schema name.")
    return f'"{schema_name}"'


def _drop_schema(engine, schema_name):
    """Drop only the generated schema after validating its exact shape."""
    quoted = _quote_schema(schema_name)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {quoted} CASCADE")


def _initialize_schema(engine, schema_name):
    """Apply the reviewed baseline and migration 005 inside one isolated schema."""
    quoted = _quote_schema(schema_name)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA {quoted}")

    try:
        raw_connection = engine.raw_connection()
        try:
            raw_connection.autocommit = True
            with raw_connection.cursor() as cursor:
                cursor.execute(f"SET search_path TO {quoted}, public")
                cursor.execute(BASELINE_PATH.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_005_PATH.read_text(encoding="utf-8"))
                # The distributable baseline deliberately targets public.
                # Override only this function setting for the disposable-schema
                # test so its trigger resolves the isolated tables.
                cursor.execute(
                    f"ALTER FUNCTION {quoted}.sync_product_quantity_from_stores() "
                    f"SET search_path TO pg_catalog, {quoted}"
                )
        finally:
            raw_connection.close()
    except Exception:
        _drop_schema(engine, schema_name)
        raise


@unittest.skipUnless(
    POSTGRES_OPT_IN and TEST_DATABASE_URL,
    "PostgreSQL integration tests require RUN_POSTGRES_INTEGRATION=1 and TEST_DATABASE_URL.",
)
class PostgreSQLIntegrationTestCase(unittest.TestCase):
    """Destructive only inside a verified random schema in a dedicated test DB."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if any(name in sys.modules for name in ("app", "models", "config")):
            raise RuntimeError(
                "Run test_postgres_integration.py in a separate Python process."
            )

        cls.test_url = _validated_test_url()
        cls.schema_name = f"makhzoon_it_{uuid4().hex}"
        cls.admin_engine = create_engine(cls.test_url, pool_pre_ping=True)
        cls.schema_created = False
        cls.original_environment = {
            key: os.environ.get(key)
            for key in ("DATABASE_URL", "SECRET_KEY", "CSRF_ENABLED")
        }

        try:
            _initialize_schema(cls.admin_engine, cls.schema_name)
            cls.schema_created = True

            # Configure the Flask-SQLAlchemy engine before app.py is imported.
            os.environ["DATABASE_URL"] = cls.test_url.render_as_string(
                hide_password=False
            )
            os.environ["SECRET_KEY"] = "postgres-integration-test-secret-only"
            os.environ["CSRF_ENABLED"] = "false"
            config_module = importlib.import_module("config")
            config_module.Config.SQLALCHEMY_DATABASE_URI = os.environ["DATABASE_URL"]
            config_module.Config.SECRET_KEY = os.environ["SECRET_KEY"]
            config_module.Config.CSRF_ENABLED = False
            config_module.Config.SQLALCHEMY_ENGINE_OPTIONS = {
                "pool_pre_ping": True,
                "connect_args": {
                    "options": (
                        f"-c timezone=UTC -c search_path={cls.schema_name},public"
                    )
                },
            }

            cls.app_module = importlib.import_module("app")
            cls.models = importlib.import_module("models")
            cls.app = cls.app_module.app
            cls.db = cls.models.db
            cls.app.config.update(TESTING=True, CSRF_ENABLED=False)
        except Exception:
            if cls.schema_created:
                _drop_schema(cls.admin_engine, cls.schema_name)
            cls.admin_engine.dispose()
            cls._restore_environment()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            if hasattr(cls, "app"):
                with cls.app.app_context():
                    cls.db.session.remove()
                    cls.db.engine.dispose()
            if getattr(cls, "schema_created", False):
                _drop_schema(cls.admin_engine, cls.schema_name)
        finally:
            if hasattr(cls, "admin_engine"):
                cls.admin_engine.dispose()
            cls._restore_environment()
            super().tearDownClass()

    @classmethod
    def _restore_environment(cls):
        for key, value in getattr(cls, "original_environment", {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.db.session.execute(text(
            """
            TRUNCATE TABLE
                login_attempts,
                password_reset_tokens,
                user_store_assignments,
                report_deliveries,
                product_history,
                order_items,
                orders,
                store_inventory,
                products,
                stores,
                users
            RESTART IDENTITY CASCADE
            """
        ))
        self.db.session.commit()

        owner = self.models.User(
            username="integration-owner",
            email="owner@integration.test",
            role="owner",
            is_active=True,
        )
        owner.set_password("Integration-Test-Password-2026!")
        store = self.models.Store(name="Integration Store", is_active=True)
        product = self.models.Product(
            name="Last Unit Product",
            category="Integration",
            supplier="Integration Supplier",
            quantity=0,
            price=Decimal("20.00"),
            purchase_price=Decimal("10.00"),
            wholesale_price=Decimal("15.00"),
            semi_wholesale_price=Decimal("18.00"),
            retail_price=Decimal("20.00"),
            reorder_level=1,
        )
        self.db.session.add_all((owner, store, product))
        self.db.session.flush()
        inventory = self.models.StoreInventory(
            store_id=store.id,
            product_id=product.id,
            quantity=1,
        )
        self.db.session.add(inventory)
        self.db.session.commit()
        self.owner_id = owner.id
        self.store_id = store.id
        self.product_id = product.id

    def tearDown(self):
        self.db.session.rollback()
        self.db.session.remove()
        self.context.pop()

    def _login(self, client):
        response = client.post(
            "/login",
            data={
                "email": "owner@integration.test",
                "password": "Integration-Test-Password-2026!",
            },
        )
        self.assertEqual(response.status_code, 302)

    def _order_payload(self, submission_token):
        return {
            "submission_token": submission_token,
            "store_id": str(self.store_id),
            "customer_name": "Concurrent Buyer",
            "customer_phone": "",
            "customer_email": "",
            "notes": "",
            "product_id": [str(self.product_id)],
            "quantity": ["1"],
            "price_tier": ["retail"],
        }

    def _concurrent_posts(self, path, payloads):
        barrier = Barrier(len(payloads))

        def submit(payload):
            client = self.app.test_client()
            self._login(client)
            barrier.wait(timeout=15)
            return client.post(path, data=payload, follow_redirects=False)

        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            futures = [executor.submit(submit, payload) for payload in payloads]
            return [future.result(timeout=30) for future in futures]

    def test_clean_baseline_and_migration_match_model_essentials(self):
        self.assertEqual(
            self.db.session.execute(text("SHOW TIME ZONE")).scalar_one().upper(),
            "UTC",
        )
        inspector = inspect(self.db.engine)
        expected_tables = set(self.db.metadata.tables)
        actual_tables = set(inspector.get_table_names(schema=self.schema_name))
        self.assertEqual(expected_tables, actual_tables)

        for table_name, model_table in self.db.metadata.tables.items():
            database_columns = {
                column["name"]
                for column in inspector.get_columns(
                    table_name, schema=self.schema_name
                )
            }
            self.assertEqual(
                set(model_table.columns.keys()), database_columns, table_name
            )

        required_columns = {
            "users": {"email", "role", "is_active", "last_login_at"},
            "orders": {
                "submission_token_hash",
                "created_by_user_id",
                "status_changed_by_user_id",
            },
            "password_reset_tokens": {"token_hash", "expires_at", "used_at"},
            "login_attempts": {
                "identity_hash",
                "remote_addr_hash",
                "attempted_at",
            },
        }
        for table_name, required in required_columns.items():
            columns = {
                column["name"]
                for column in inspector.get_columns(
                    table_name, schema=self.schema_name
                )
            }
            self.assertTrue(required.issubset(columns), table_name)

        assignment_pk = inspector.get_pk_constraint(
            "user_store_assignments", schema=self.schema_name
        )
        self.assertEqual(
            set(assignment_pk["constrained_columns"]), {"user_id", "store_id"}
        )
        trigger_and_function = self.db.session.execute(text(
            """
            SELECT
                EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = :schema
                      AND p.proname = 'sync_product_quantity_from_stores'
                ),
                EXISTS (
                    SELECT 1
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema
                      AND c.relname = 'store_inventory'
                      AND t.tgname = 'trg_sync_product_quantity'
                      AND NOT t.tgisinternal
                )
            """
        ), {"schema": self.schema_name}).one()
        self.assertEqual(trigger_and_function, (True, True))

    def test_direct_inventory_write_keeps_product_aggregate_synchronized(self):
        self.db.session.execute(text(
            """
            UPDATE store_inventory
            SET quantity = 7
            WHERE store_id = :store_id AND product_id = :product_id
            """
        ), {"store_id": self.store_id, "product_id": self.product_id})
        self.db.session.commit()
        aggregate = self.db.session.execute(text(
            "SELECT quantity FROM products WHERE id = :product_id"
        ), {"product_id": self.product_id}).scalar_one()
        self.assertEqual(aggregate, 7)

    def test_concurrent_orders_cannot_oversell_last_unit(self):
        responses = self._concurrent_posts(
            "/orders/add",
            [
                self._order_payload(secrets.token_urlsafe(32)),
                self._order_payload(secrets.token_urlsafe(32)),
            ],
        )
        self.assertEqual(sorted(response.status_code for response in responses), [200, 302])
        self.db.session.expire_all()
        inventory = self.models.StoreInventory.query.filter_by(
            store_id=self.store_id, product_id=self.product_id
        ).one()
        product = self.db.session.get(self.models.Product, self.product_id)
        self.assertEqual(self.models.Order.query.count(), 1)
        self.assertEqual(inventory.quantity, 0)
        self.assertEqual(product.quantity, 0)
        sale = self.models.ProductHistory.query.filter_by(event_type="sale").one()
        self.assertEqual(sale.quantity_change, -1)
        self.assertEqual(sale.old_quantity, 1)
        self.assertEqual(sale.new_quantity, 0)

    def test_concurrent_duplicate_submission_creates_one_order(self):
        shared_token = secrets.token_urlsafe(32)
        responses = self._concurrent_posts(
            "/orders/add",
            [
                self._order_payload(shared_token),
                self._order_payload(shared_token),
            ],
        )
        self.assertTrue(all(response.status_code == 302 for response in responses))
        self.db.session.expire_all()
        inventory = self.models.StoreInventory.query.filter_by(
            store_id=self.store_id, product_id=self.product_id
        ).one()
        self.assertEqual(self.models.Order.query.count(), 1)
        self.assertEqual(inventory.quantity, 0)
        self.assertEqual(
            self.models.ProductHistory.query.filter_by(event_type="sale").count(), 1
        )

    def test_concurrent_cancellation_restores_inventory_once(self):
        client = self.app.test_client()
        self._login(client)
        created = client.post(
            "/orders/add",
            data=self._order_payload(secrets.token_urlsafe(32)),
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 302)
        order_id = self.models.Order.query.one().id

        responses = self._concurrent_posts(
            f"/orders/{order_id}/cancel",
            [{}, {}],
        )
        self.assertTrue(all(response.status_code == 302 for response in responses))
        self.db.session.expire_all()
        order = self.db.session.get(self.models.Order, order_id)
        inventory = self.models.StoreInventory.query.filter_by(
            store_id=self.store_id, product_id=self.product_id
        ).one()
        self.assertEqual(order.status, "Cancelled")
        self.assertTrue(order.inventory_restored)
        self.assertEqual(inventory.quantity, 1)
        restoration = self.models.ProductHistory.query.filter_by(
            event_type="sale_cancelled"
        ).one()
        self.assertEqual(restoration.quantity_change, 1)
        self.assertEqual(restoration.old_quantity, 0)
        self.assertEqual(restoration.new_quantity, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
