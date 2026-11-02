"""Group 3 integration tests using an isolated in-memory database.

The configured PostgreSQL database is intentionally replaced before importing
the application so automated tests can never modify real inventory data.
"""

import unittest
import secrets
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

from config import Config

Config.SQLALCHEMY_DATABASE_URI = "sqlite://"
Config.TESTING = True
Config.SECRET_KEY = "orders-test-secret-not-for-production"
Config.CSRF_ENABLED = False

from app import app, db  # noqa: E402
from models import Order, OrderItem, Product, Store, StoreInventory, User  # noqa: E402


class OrderManagementTestCase(unittest.TestCase):
    """Exercise order routes, business rules, and cross-group regressions."""

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = app.test_client()
        self.store = Store(name="Main Store")
        self.owner = User(
            username="owner", email="owner@example.test", role="owner", is_active=True
        )
        self.owner.set_password("test-owner-password")
        db.session.add_all([self.store, self.owner])
        db.session.commit()
        self.client.post(
            "/login",
            data={"email": self.owner.email, "password": "test-owner-password"},
        )

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def add_product(self, name="Mouse", quantity=10, price=100.0):
        product = Product(
            name=name,
            category="Accessories",
            supplier="Supplier One",
            price=price,
            purchase_price=price / 2,
            wholesale_price=price,
            semi_wholesale_price=price,
            retail_price=price,
            quantity=quantity,
            reorder_level=2,
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(StoreInventory(store=self.store, product=product, quantity=quantity))
        db.session.commit()
        return product

    def order_data(self, items, **overrides):
        data = {
            "submission_token": secrets.token_urlsafe(32),
            "customer_name": "Kareem",
            "customer_phone": "01000000000",
            "customer_email": "kareem@example.com",
            "notes": "Counter sale",
            "store_id": str(self.store.id),
            "product_id": [str(product_id) for product_id, _ in items],
            "quantity": [str(quantity) for _, quantity in items],
            "price_tier": ["retail" for _ in items],
        }
        data.update(overrides)
        return data

    def create_order(self, items, **overrides):
        return self.client.post(
            "/orders/add",
            data=self.order_data(items, **overrides),
        )

    def test_orders_list_empty_existing_search_filter_sort_and_invalid_params(self):
        empty = self.client.get("/orders")
        self.assertEqual(empty.status_code, 200)
        self.assertIn("No orders yet", empty.get_data(as_text=True))

        alpha = self.add_product("Alpha", price=10)
        zulu = self.add_product("Zulu", price=20)
        self.create_order([(alpha.id, 1)], customer_name="Alice")
        first = Order.query.one()
        first.order_date = datetime(2026, 1, 5, 10, 0)
        db.session.commit()
        self.client.post(f"/orders/{first.id}/status", data={"status": "Processing"})
        self.create_order([(zulu.id, 2)], customer_name="Zed")
        second = Order.query.filter(Order.id != first.id).one()
        second.order_date = datetime(2026, 2, 5, 10, 0)
        db.session.commit()

        by_id = self.client.get(f"/orders?search={first.id}").get_data(as_text=True)
        self.assertIn("Alice", by_id)
        by_name = self.client.get("/orders?search=zed").get_data(as_text=True)
        self.assertIn("Zed", by_name)
        self.assertNotIn("Alice", by_name)

        date_filtered = self.client.get(
            "/orders?date_from=2026-02-01&date_to=2026-02-28"
        ).get_data(as_text=True)
        self.assertIn("Zed", date_filtered)
        self.assertNotIn("Alice", date_filtered)

        filtered = self.client.get("/orders?status=Processing").get_data(as_text=True)
        self.assertIn("Alice", filtered)
        self.assertNotIn("Zed", filtered)

        ascending = self.client.get(
            "/orders?sort=customer_name&direction=asc"
        ).get_data(as_text=True)
        descending = self.client.get(
            "/orders?sort=customer_name&direction=desc"
        ).get_data(as_text=True)
        self.assertLess(ascending.find("Alice"), ascending.find("Zed"))
        self.assertLess(descending.find("Zed"), descending.find("Alice"))

        invalid = self.client.get(
            "/orders?status=Hacked&sort=DROP TABLE&direction=sideways&date_from=nope"
        )
        self.assertEqual(invalid.status_code, 200)
        no_results = self.client.get("/orders?search=nobody")
        self.assertIn("No matching orders", no_results.get_data(as_text=True))

    def test_valid_one_product_order_uses_database_price_and_snapshot(self):
        product = self.add_product(price=125.75)
        response = self.client.post(
            "/orders/add",
            data=self.order_data(
                [(product.id, 2)],
                unit_price="0.01",
                grand_total="0.02",
            ),
        )
        self.assertEqual(response.status_code, 302)

        order = Order.query.one()
        item = OrderItem.query.one()
        db.session.refresh(product)
        self.assertEqual(order.status, "Pending")
        self.assertFalse(order.inventory_restored)
        self.assertEqual(product.quantity, 8)
        self.assertEqual(item.unit_price, Decimal("125.75"))
        self.assertEqual(order.get_total(), Decimal("251.50"))

        product.price = 999.0
        db.session.commit()
        self.assertEqual(order.get_total(), Decimal("251.50"))

    def test_valid_multi_product_order_reduces_every_stock_once(self):
        mouse = self.add_product("Mouse", quantity=10, price=25)
        keyboard = self.add_product("Keyboard", quantity=6, price=50)
        response = self.create_order([(mouse.id, 2), (keyboard.id, 3)])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.query.count(), 1)
        self.assertEqual(OrderItem.query.count(), 2)
        db.session.refresh(mouse)
        db.session.refresh(keyboard)
        self.assertEqual(mouse.quantity, 8)
        self.assertEqual(keyboard.quantity, 3)
        self.assertEqual(Order.query.one().get_total(), Decimal("200.00"))

    def test_out_of_stock_products_remain_visible_but_disabled(self):
        product = self.add_product("Unavailable Cable", quantity=0)
        response = self.client.get("/orders/add")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unavailable Cable", body)
        self.assertIn("Out of Stock", body)
        self.assertIn(f'value="{product.id}"', body)
        self.assertRegex(body, rf'value="{product.id}"[\s\S]*?disabled')

    def test_create_order_validation_rejects_empty_and_invalid_quantities(self):
        product = self.add_product()

        missing_customer = self.create_order([(product.id, 1)], customer_name="")
        self.assertIn("Customer Name is required", missing_customer.get_data(as_text=True))

        for quantity, expected in (
            ("0", "greater than 0"),
            ("-1", "greater than 0"),
            ("1.5", "whole-number"),
        ):
            response = self.create_order([(product.id, quantity)])
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, response.get_data(as_text=True))

        empty = self.client.post(
            "/orders/add",
            data={
                "submission_token": secrets.token_urlsafe(32),
                "customer_name": "Kareem", "product_id": [], "quantity": []
            },
        )
        self.assertEqual(empty.status_code, 200)
        self.assertIn("order was not created", empty.get_data(as_text=True))
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(product.quantity, 10)

    def test_invalid_product_duplicate_and_insufficient_stock_are_atomic(self):
        mouse = self.add_product("Mouse", quantity=10)
        keyboard = self.add_product("Keyboard", quantity=1)

        invalid = self.create_order([(999999, 1)])
        self.assertIn("no longer exists", invalid.get_data(as_text=True))

        duplicate = self.create_order([(mouse.id, 1), (mouse.id, 1)])
        self.assertIn("already included", duplicate.get_data(as_text=True))

        insufficient = self.create_order([(mouse.id, 2), (keyboard.id, 2)])
        body = insufficient.get_data(as_text=True)
        self.assertIn("Only 1 unit is available", body)
        self.assertIn(f'value="{mouse.id}"', body)
        self.assertIn('value="2"', body)
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(OrderItem.query.count(), 0)
        db.session.refresh(mouse)
        db.session.refresh(keyboard)
        self.assertEqual(mouse.quantity, 10)
        self.assertEqual(keyboard.quantity, 1)

    def test_database_failure_rolls_back_order_items_and_stock(self):
        mouse = self.add_product("Mouse", quantity=10)
        keyboard = self.add_product("Keyboard", quantity=10)

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("simulated failure"),
        ):
            response = self.create_order([(mouse.id, 2), (keyboard.id, 3)])

        self.assertEqual(response.status_code, 200)
        self.assertIn("No stock was changed", response.get_data(as_text=True))
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(OrderItem.query.count(), 0)
        db.session.refresh(mouse)
        db.session.refresh(keyboard)
        self.assertEqual(mouse.quantity, 10)
        self.assertEqual(keyboard.quantity, 10)

    def test_status_transitions_and_terminal_protection(self):
        product = self.add_product()
        self.create_order([(product.id, 1)])
        order = Order.query.one()

        self.assertEqual(
            self.client.get(f"/orders/{order.id}/status").status_code,
            405,
        )
        invalid_name = self.client.post(
            f"/orders/{order.id}/status", data={"status": "Shipped"}
        )
        self.assertEqual(invalid_name.status_code, 302)
        db.session.refresh(order)
        self.assertEqual(order.status, "Pending")

        invalid_jump = self.client.post(
            f"/orders/{order.id}/status", data={"status": "Completed"}
        )
        self.assertEqual(invalid_jump.status_code, 302)
        db.session.refresh(order)
        self.assertEqual(order.status, "Pending")

        self.client.post(
            f"/orders/{order.id}/status", data={"status": "Processing"}
        )
        db.session.refresh(order)
        self.assertEqual(order.status, "Processing")
        self.client.post(
            f"/orders/{order.id}/status", data={"status": "Completed"}
        )
        db.session.refresh(order)
        self.assertEqual(order.status, "Completed")

        terminal = self.client.post(
            f"/orders/{order.id}/status", data={"status": "Cancelled"}
        )
        self.assertEqual(terminal.status_code, 302)
        db.session.refresh(order)
        db.session.refresh(product)
        self.assertEqual(order.status, "Completed")
        self.assertEqual(product.quantity, 9)

    def test_cancellation_restores_stock_exactly_once(self):
        product = self.add_product(quantity=10)
        self.create_order([(product.id, 4)])
        order = Order.query.one()
        db.session.refresh(product)
        self.assertEqual(product.quantity, 6)

        self.assertEqual(
            self.client.get(f"/orders/{order.id}/cancel").status_code,
            405,
        )
        cancelled = self.client.post(f"/orders/{order.id}/cancel")
        self.assertEqual(cancelled.status_code, 302)
        db.session.refresh(order)
        db.session.refresh(product)
        self.assertEqual(order.status, "Cancelled")
        self.assertTrue(order.inventory_restored)
        self.assertEqual(product.quantity, 10)

        repeated = self.client.post(f"/orders/{order.id}/cancel")
        self.assertEqual(repeated.status_code, 302)
        db.session.refresh(product)
        self.assertEqual(product.quantity, 10)
        self.assertEqual(OrderItem.query.count(), 1)

    def test_status_cancellation_restores_stock_and_failure_rolls_back(self):
        product = self.add_product(quantity=10)
        self.create_order([(product.id, 3)])
        order = Order.query.one()

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("simulated failure"),
        ):
            failed = self.client.post(f"/orders/{order.id}/cancel")
        self.assertEqual(failed.status_code, 302)
        db.session.refresh(order)
        db.session.refresh(product)
        self.assertEqual(order.status, "Pending")
        self.assertFalse(order.inventory_restored)
        self.assertEqual(product.quantity, 7)

        via_status = self.client.post(
            f"/orders/{order.id}/status", data={"status": "Cancelled"}
        )
        self.assertEqual(via_status.status_code, 302)
        db.session.refresh(order)
        db.session.refresh(product)
        self.assertEqual(order.status, "Cancelled")
        self.assertTrue(order.inventory_restored)
        self.assertEqual(product.quantity, 10)

    def test_order_details_show_historical_lines_and_state_routes_404(self):
        product = self.add_product("Historical Mouse", quantity=5, price=42.5)
        self.create_order([(product.id, 2)])
        order = Order.query.one()

        details = self.client.get(f"/orders/{order.id}")
        body = details.get_data(as_text=True)
        self.assertEqual(details.status_code, 200)
        self.assertIn("Historical Mouse", body)
        self.assertIn("42.50", body)
        self.assertIn("85.00", body)
        self.assertEqual(
            self.client.post(
                "/orders/999999/status", data={"status": "Processing"}
            ).status_code,
            404,
        )

    def test_details_404_routes_and_group_one_two_regressions(self):
        self.assertEqual(self.client.get("/orders/999999").status_code, 404)
        self.assertEqual(self.client.post("/orders/999999/cancel").status_code, 404)
        self.assertEqual(self.client.get("/orders/add").status_code, 200)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/products").status_code, 200)
        self.assertEqual(self.client.get("/products/add").status_code, 200)

        product = self.add_product()
        edit = self.client.post(
            f"/products/{product.id}/edit",
            data={
                "name": "Updated Mouse",
                "category": product.category,
                "supplier": product.supplier,
                "purchase_price": "60",
                "wholesale_price": "100",
                "semi_wholesale_price": "110",
                "retail_price": "120",
                f"inventory_{self.store.id}": "10",
                "adjustment_type": "correction",
                "reorder_level": "2",
            },
        )
        self.assertEqual(edit.status_code, 302)

        self.create_order([(product.id, 1)])
        protected = self.client.post(f"/products/{product.id}/delete")
        self.assertEqual(protected.status_code, 302)
        self.assertIsNotNone(db.session.get(Product, product.id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
