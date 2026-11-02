"""Security, authentication, authorization, and idempotency regressions.

These tests deliberately use an isolated in-memory SQLite database. PostgreSQL
concurrency and trigger behavior are covered by the separate PostgreSQL suite.
CSRF remains enabled here so authentication and authorization tests exercise
the same state-change guard used by the application.
"""

import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from config import Config


# Configure a disposable database before importing the central application.
Config.SQLALCHEMY_DATABASE_URI = "sqlite://"
Config.SECRET_KEY = "security-tests-only-not-for-production"
Config.TESTING = True
Config.CSRF_ENABLED = True

from app import app, db, token_digest  # noqa: E402
from models import (  # noqa: E402
    Order,
    OrderItem,
    PasswordResetToken,
    Product,
    ProductHistory,
    Store,
    StoreInventory,
    User,
    utc_now,
)


class SecurityAuthenticationTestCase(unittest.TestCase):
    """Exercise security controls without bypassing CSRF or sending email."""

    _CONFIG_KEYS = (
        "TESTING",
        "SECRET_KEY",
        "CSRF_ENABLED",
        "LOGIN_MAX_ATTEMPTS",
        "LOGIN_ATTEMPT_WINDOW_MINUTES",
        "PASSWORD_RESET_MINUTES",
        "PASSWORD_MIN_LENGTH",
        "PUBLIC_BASE_URL",
        "PERMANENT_SESSION_LIFETIME",
        "ENABLE_AUTH_EMAIL",
        "SMTP_HOST",
        "SMTP_FROM",
    )

    def setUp(self):
        self.original_config = {
            key: app.config.get(key) for key in self._CONFIG_KEYS
        }
        app.config.update(
            TESTING=True,
            SECRET_KEY="security-tests-only-not-for-production",
            CSRF_ENABLED=True,
            LOGIN_MAX_ATTEMPTS=3,
            LOGIN_ATTEMPT_WINDOW_MINUTES=15,
            PASSWORD_RESET_MINUTES=60,
            PASSWORD_MIN_LENGTH=12,
            PUBLIC_BASE_URL="https://makhzoon.example.test",
            PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
            ENABLE_AUTH_EMAIL=True,
            SMTP_HOST="smtp.example.test",
            SMTP_FROM="no-reply@example.test",
        )
        if not str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith(
            "sqlite"
        ):
            raise RuntimeError(
                "Security tests refuse to run outside their isolated SQLite database."
            )

        self.app_context = app.app_context()
        self.app_context.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

        self.assigned_store = Store(name="Assigned Store", is_active=True)
        self.other_store = Store(name="Other Store", is_active=True)
        self.owner = User(
            username="owner",
            email="owner@example.test",
            role="owner",
            is_active=True,
        )
        self.owner.set_password("OwnerPassword!123")
        self.staff = User(
            username="staff",
            email="staff@example.test",
            role="staff",
            is_active=True,
        )
        self.staff.set_password("StaffPassword!123")
        self.inactive_staff = User(
            username="inactive",
            email="inactive@example.test",
            role="staff",
            is_active=False,
        )
        self.inactive_staff.set_password("InactivePass!123")
        self.staff.assigned_stores.append(self.assigned_store)

        self.product = Product(
            name="Security Test Product",
            category="Tests",
            supplier="Test Supplier",
            quantity=5,
            price=Decimal("325.00"),
            purchase_price=Decimal("137.42"),
            wholesale_price=Decimal("275.00"),
            semi_wholesale_price=Decimal("300.00"),
            retail_price=Decimal("325.00"),
            reorder_level=1,
        )
        db.session.add_all(
            [
                self.assigned_store,
                self.other_store,
                self.owner,
                self.staff,
                self.inactive_staff,
                self.product,
            ]
        )
        db.session.flush()
        db.session.add_all(
            [
                StoreInventory(
                    store=self.assigned_store, product=self.product, quantity=3
                ),
                StoreInventory(
                    store=self.other_store, product=self.product, quantity=2
                ),
            ]
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        app.config.update(self.original_config)

    def csrf_token(self, client=None):
        """Return the session token, seeding one without disabling the guard."""
        client = client or self.client
        with client.session_transaction() as session_state:
            token = session_state.get("csrf_token")
            if not token:
                token = "csrf-test-token"
                session_state["csrf_token"] = token
            return token

    def post_csrf(self, path, data=None, client=None, token=None, **kwargs):
        client = client or self.client
        payload = dict(data or {})
        payload["csrf_token"] = token or self.csrf_token(client)
        return client.post(path, data=payload, **kwargs)

    def login(self, user=None, password=None, client=None):
        user = user or self.owner
        password = password or (
            "OwnerPassword!123" if user.is_owner else "StaffPassword!123"
        )
        response = self.post_csrf(
            "/login",
            {"email": user.email, "password": password},
            client=client,
        )
        self.assertEqual(response.status_code, 302)
        return response

    def make_order(self, store=None, customer_name="Assigned Customer"):
        store = store or self.assigned_store
        order = Order(
            store=store,
            customer_name=customer_name,
            created_by_user=self.owner,
            status_changed_by_user=self.owner,
        )
        order.items.append(
            OrderItem(
                product=self.product,
                quantity=1,
                price_tier="retail",
                unit_price=Decimal("325.00"),
                purchase_unit_cost=Decimal("137.42"),
            )
        )
        db.session.add(order)
        db.session.commit()
        return order

    def order_payload(self, store, submission_token):
        return {
            "submission_token": submission_token,
            "store_id": str(store.id),
            "customer_name": "Idempotent Customer",
            "customer_phone": "",
            "customer_email": "",
            "notes": "",
            "product_id": [str(self.product.id)],
            "quantity": ["1"],
            "price_tier": ["retail"],
        }

    def test_csrf_rejects_missing_and_invalid_tokens_and_accepts_valid_token(self):
        credentials = {
            "email": self.owner.email,
            "password": "OwnerPassword!123",
        }
        missing = self.client.post("/login", data=credentials)
        self.assertEqual(missing.status_code, 400)

        self.csrf_token()
        invalid = self.client.post(
            "/login", data={**credentials, "csrf_token": "invalid-token"}
        )
        self.assertEqual(invalid.status_code, 400)

        valid = self.post_csrf("/login", credentials)
        self.assertEqual(valid.status_code, 302)

    def test_unauthenticated_html_redirects_and_api_returns_401(self):
        html = self.client.get("/dashboard")
        self.assertEqual(html.status_code, 302)
        self.assertIn("/login", html.headers["Location"])

        api = self.client.get(f"/api/store-inventory/{self.assigned_store.id}")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.get_json(), {"error": "authentication_required"})

    def test_staff_receives_403_for_owner_only_routes(self):
        self.login(self.staff)
        for path in (
            "/dashboard", "/products", "/products/add", "/inventory",
            "/reports", "/reports/pdf", "/users",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(
            self.post_csrf("/stores/add", {"name": "Forbidden Store"}).status_code,
            403,
        )

    def test_staff_access_is_limited_to_assigned_store_and_orders(self):
        assigned_order = self.make_order(customer_name="Visible Assigned Customer")
        other_order = self.make_order(
            self.other_store, customer_name="Hidden Other Customer"
        )
        self.login(self.staff)

        assigned_api = self.client.get(
            f"/api/store-inventory/{self.assigned_store.id}"
        )
        self.assertEqual(assigned_api.status_code, 200)
        self.assertEqual(
            assigned_api.get_json()[str(self.product.id)], 3
        )
        self.assertEqual(
            self.client.get(f"/api/store-inventory/{self.other_store.id}").status_code,
            403,
        )

        self.assertEqual(
            self.client.get(f"/orders/{assigned_order.id}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/orders/{other_order.id}").status_code, 403
        )
        listing = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("Visible Assigned Customer", listing)
        self.assertNotIn("Hidden Other Customer", listing)

    def test_staff_cannot_submit_an_unauthorized_store_id(self):
        self.login(self.staff)
        before = StoreInventory.query.filter_by(
            store_id=self.other_store.id, product_id=self.product.id
        ).one().quantity
        response = self.post_csrf(
            "/orders/add",
            self.order_payload(
                self.other_store, "unauthorized-store-token-" + "x" * 32
            ),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.query.count(), 0)
        after = StoreInventory.query.filter_by(
            store_id=self.other_store.id, product_id=self.product.id
        ).one().quantity
        self.assertEqual(after, before)

    def test_staff_responses_do_not_expose_purchase_cost(self):
        order = self.make_order()
        self.login(self.staff)

        form_body = self.client.get("/orders/add").get_data(as_text=True)
        detail_body = self.client.get(f"/orders/{order.id}").get_data(as_text=True)
        for body in (form_body, detail_body):
            self.assertNotIn("137.42", body)
            self.assertNotIn("purchase_price", body)
        self.assertNotIn("Cost snapshot", detail_body)

    def test_inactive_staff_cannot_login_or_continue_an_existing_session(self):
        self.login(self.staff)
        self.staff.is_active = False
        db.session.commit()

        established = self.client.get("/orders")
        self.assertEqual(established.status_code, 302)
        self.assertIn("/login", established.headers["Location"])

        another_client = app.test_client()
        inactive_login = self.post_csrf(
            "/login",
            {"email": self.staff.email, "password": "StaffPassword!123"},
            client=another_client,
        )
        self.assertEqual(inactive_login.status_code, 401)

    def test_staff_status_changes_are_forward_only_and_cancellation_is_forbidden(self):
        order = self.make_order()
        self.login(self.staff)

        forward = self.post_csrf(
            f"/orders/{order.id}/status", {"status": "Processing"}
        )
        self.assertEqual(forward.status_code, 302)
        db.session.refresh(order)
        self.assertEqual(order.status, "Processing")
        self.assertEqual(order.status_changed_by_user_id, self.staff.id)

        backward = self.post_csrf(
            f"/orders/{order.id}/status", {"status": "Pending"}
        )
        self.assertEqual(backward.status_code, 302)
        db.session.refresh(order)
        self.assertEqual(order.status, "Processing")

        completed = self.post_csrf(
            f"/orders/{order.id}/status", {"status": "Completed"}
        )
        self.assertEqual(completed.status_code, 302)
        db.session.refresh(order)
        self.assertEqual(order.status, "Completed")

        cancelled = self.post_csrf(f"/orders/{order.id}/cancel")
        self.assertEqual(cancelled.status_code, 403)
        db.session.refresh(order)
        self.assertEqual(order.status, "Completed")

    def test_login_rate_limit_blocks_after_configured_failures(self):
        invalid = {"email": self.owner.email, "password": "wrong-password"}
        for _ in range(app.config["LOGIN_MAX_ATTEMPTS"]):
            response = self.post_csrf("/login", invalid)
            self.assertEqual(response.status_code, 401)

        limited = self.post_csrf("/login", invalid)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Too many sign-in attempts", limited.get_data(as_text=True))

    def test_password_reset_email_is_mocked_tokens_expire_and_are_single_use(self):
        with patch("app.send_password_reset_email", side_effect=OSError("offline")):
            failed_delivery = self.post_csrf(
                "/forgot-password", {"email": self.owner.email}
            )
        self.assertEqual(failed_delivery.status_code, 302)
        self.assertEqual(
            PasswordResetToken.query.filter_by(user_id=self.owner.id).count(), 0
        )

        with patch("app.send_password_reset_email") as sender:
            requested = self.post_csrf(
                "/forgot-password", {"email": self.staff.email}
            )
            self.assertEqual(requested.status_code, 302)
            sender.assert_called_once()
            expired_token = sender.call_args.args[1].rsplit("/", 1)[-1]

            record = PasswordResetToken.query.filter_by(
                token_hash=token_digest(expired_token)
            ).one()
            record.created_at = utc_now() - timedelta(hours=2)
            record.expires_at = utc_now() - timedelta(hours=1)
            db.session.commit()
            self.assertEqual(
                self.client.get(f"/reset-password/{expired_token}").status_code,
                400,
            )

            sender.reset_mock()
            self.post_csrf("/forgot-password", {"email": self.staff.email})
            sender.assert_called_once()
            usable_token = sender.call_args.args[1].rsplit("/", 1)[-1]

        changed = self.post_csrf(
            f"/reset-password/{usable_token}",
            {
                "password": "UpdatedPassword!456",
                "password_confirmation": "UpdatedPassword!456",
            },
        )
        self.assertEqual(changed.status_code, 302)
        db.session.refresh(self.staff)
        self.assertTrue(self.staff.check_password("UpdatedPassword!456"))

        reused = self.post_csrf(
            f"/reset-password/{usable_token}",
            {
                "password": "AnotherPassword!789",
                "password_confirmation": "AnotherPassword!789",
            },
        )
        self.assertEqual(reused.status_code, 400)

    def test_successful_login_creates_a_permanent_twelve_hour_session(self):
        self.assertEqual(app.permanent_session_lifetime, timedelta(hours=12))
        self.login(self.owner)
        with self.client.session_transaction() as session_state:
            self.assertTrue(session_state.permanent)
            self.assertEqual(int(session_state["_user_id"]), self.owner.id)

    def test_owner_cli_creation_and_recovery_use_hidden_prompts(self):
        self.owner.role = "staff"
        db.session.commit()
        runner = app.test_cli_runner()
        initial_password = "CliOwnerPassword!123"
        created_result = runner.invoke(
            args=[
                "create-owner", "--email", "cli.owner@example.test",
                "--username", "cli-owner",
            ],
            input=f"{initial_password}\n{initial_password}\n",
        )
        self.assertEqual(created_result.exit_code, 0, created_result.output)
        self.assertNotIn(initial_password, created_result.output)
        created = User.query.filter_by(email="cli.owner@example.test").one()
        self.assertTrue(created.is_owner)
        self.assertTrue(created.check_password(initial_password))

        replacement = "RecoveredOwnerPass!456"
        reset_result = runner.invoke(
            args=["reset-owner-password", "--email", created.email],
            input=f"{replacement}\n{replacement}\n",
        )
        self.assertEqual(reset_result.exit_code, 0, reset_result.output)
        self.assertNotIn(replacement, reset_result.output)
        db.session.refresh(created)
        self.assertTrue(created.check_password(replacement))

    def test_owner_can_create_staff_and_assign_active_stores(self):
        self.login(self.owner)
        response = self.post_csrf(
            "/users/add",
            {
                "email": "new.staff@example.test",
                "username": "new-staff",
                "password": "NewStaffPass!123",
                "password_confirmation": "NewStaffPass!123",
                "store_ids": [str(self.assigned_store.id)],
                "role": "owner",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = User.query.filter_by(email="new.staff@example.test").one()
        self.assertEqual(created.role, "staff")
        self.assertTrue(created.is_active)
        self.assertTrue(created.check_password("NewStaffPass!123"))
        self.assertEqual(
            {store.id for store in created.assigned_stores},
            {self.assigned_store.id},
        )

    def test_owner_can_deactivate_reactivate_and_initiate_staff_reset(self):
        self.login(self.owner)
        deactivated = self.post_csrf(
            f"/users/{self.staff.id}/toggle-active"
        )
        self.assertEqual(deactivated.status_code, 302)
        db.session.refresh(self.staff)
        self.assertFalse(self.staff.is_active)

        reactivated = self.post_csrf(
            f"/users/{self.staff.id}/toggle-active"
        )
        self.assertEqual(reactivated.status_code, 302)
        db.session.refresh(self.staff)
        self.assertTrue(self.staff.is_active)

        with patch("app.send_password_reset_email") as sender:
            reset = self.post_csrf(f"/users/{self.staff.id}/reset-password")
        self.assertEqual(reset.status_code, 302)
        sender.assert_called_once()
        self.assertEqual(
            PasswordResetToken.query.filter_by(user_id=self.staff.id).count(), 1
        )

    def test_sequential_duplicate_submission_creates_and_deducts_once(self):
        self.login(self.staff)
        raw_token = "duplicate-submission-token-" + "x" * 32
        payload = self.order_payload(self.assigned_store, raw_token)

        first = self.post_csrf("/orders/add", payload)
        second = self.post_csrf("/orders/add", payload)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first.headers["Location"], second.headers["Location"])

        self.assertEqual(Order.query.count(), 1)
        order = Order.query.one()
        self.assertEqual(order.submission_token_hash, token_digest(raw_token))
        self.assertEqual(order.created_by_user_id, self.staff.id)
        inventory = StoreInventory.query.filter_by(
            store_id=self.assigned_store.id, product_id=self.product.id
        ).one()
        self.assertEqual(inventory.quantity, 2)
        self.assertEqual(
            ProductHistory.query.filter_by(event_type="sale").count(), 1
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
