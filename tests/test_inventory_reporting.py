"""End-to-end coverage for stores, history, tiers, reports, PDF, and email."""

import unittest
import secrets
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from config import Config

Config.SQLALCHEMY_DATABASE_URI = "sqlite://"
Config.TESTING = True
Config.SECRET_KEY = "inventory-test-secret-not-for-production"
Config.CSRF_ENABLED = False

from app import app, db  # noqa: E402
from models import Order, OrderItem, Product, ProductHistory, ReportDelivery, Store, StoreInventory, User  # noqa: E402
from report_scheduler import deliver  # noqa: E402


class InventoryReportingTestCase(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.create_all()
        self.client = app.test_client()
        self.store_a = Store(name="Store A")
        self.store_b = Store(name="Store B")
        self.owner = User(
            username="owner", email="owner@example.test", role="owner", is_active=True
        )
        self.owner.set_password("test-owner-password")
        db.session.add_all([self.store_a, self.store_b, self.owner])
        db.session.commit()
        self.client.post(
            "/login",
            data={"email": self.owner.email, "password": "test-owner-password"},
        )

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def product_payload(self, **updates):
        data = {
            "name": "Wireless Mouse", "category": "Accessories", "supplier": "Supplier",
            "purchase_price": "200", "wholesale_price": "260",
            "semi_wholesale_price": "280", "retail_price": "300",
            "reorder_level": "3", "adjustment_type": "purchase",
            f"inventory_{self.store_a.id}": "4", f"inventory_{self.store_b.id}": "5",
        }
        data.update(updates)
        return data

    def create_product(self):
        response = self.client.post("/products/add", data=self.product_payload())
        self.assertEqual(response.status_code, 302)
        return Product.query.one()

    def test_product_store_stock_prices_and_history(self):
        product = self.create_product()
        self.assertEqual(product.quantity, 9)
        self.assertEqual(product.retail_price, Decimal("300.00"))
        self.assertEqual(len(product.store_inventories), 2)
        self.assertEqual(ProductHistory.query.filter_by(event_type="created").count(), 1)
        self.assertEqual(ProductHistory.query.filter_by(event_type="restock").count(), 2)

        payload = self.product_payload(
            retail_price="325", adjustment_type="correction",
            **{f"inventory_{self.store_b.id}": "7"},
        )
        response = self.client.post(f"/products/{product.id}/edit", data=payload)
        self.assertEqual(response.status_code, 302)
        db.session.refresh(product)
        self.assertEqual(product.quantity, 11)
        self.assertEqual(ProductHistory.query.filter_by(event_type="price_change").count(), 1)
        correction = ProductHistory.query.filter_by(event_type="stock_correction").one()
        self.assertIsNone(correction.purchase_unit_cost)

    def test_each_tier_uses_selected_store_and_cancellation_restores_once(self):
        product = self.create_product()
        data = {
            "submission_token": secrets.token_urlsafe(32),
            "store_id": str(self.store_a.id), "customer_name": "Customer",
            "product_id": [str(product.id)], "quantity": ["2"], "price_tier": ["wholesale"],
        }
        response = self.client.post("/orders/add", data=data)
        self.assertEqual(response.status_code, 302)
        order, item = Order.query.one(), OrderItem.query.one()
        self.assertEqual(item.price_tier, "wholesale")
        self.assertEqual(item.unit_price, Decimal("260.00"))
        self.assertEqual(item.purchase_unit_cost, Decimal("200.00"))
        self.assertEqual(StoreInventory.query.filter_by(store_id=self.store_a.id, product_id=product.id).one().quantity, 2)
        self.assertEqual(StoreInventory.query.filter_by(store_id=self.store_b.id, product_id=product.id).one().quantity, 5)

        self.client.post(f"/orders/{order.id}/cancel")
        self.client.post(f"/orders/{order.id}/cancel")
        self.assertEqual(StoreInventory.query.filter_by(store_id=self.store_a.id, product_id=product.id).one().quantity, 4)
        self.assertEqual(ProductHistory.query.filter_by(event_type="sale_cancelled").count(), 1)

    def test_inventory_dashboard_reports_and_pdf(self):
        product = self.create_product()
        self.client.post("/orders/add", data={
            "submission_token": secrets.token_urlsafe(32),
            "store_id": str(self.store_b.id), "customer_name": "Retail Buyer",
            "product_id": [str(product.id)], "quantity": ["1"], "price_tier": ["retail"],
        })
        for path, marker in (
            ("/inventory", "Known cumulative purchase cost"),
            (f"/inventory/{product.id}/history", "Purchased inventory"),
            ("/dashboard", "Monthly Sales"),
            ("/reports?period=five_days", "Smart insights"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(marker, response.get_data(as_text=True))
        pdf = self.client.get("/reports/pdf?period=five_days")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_scheduled_delivery_is_idempotent_and_mocked(self):
        self.create_product()
        app.config.update(ENABLE_REPORT_EMAIL=True, REPORT_EMAIL_TO="owner@example.com")
        with patch("report_scheduler.send_report_email") as send:
            first = deliver("five_day", date(2026, 10, 20), date(2026, 10, 24))
            second = deliver("five_day", date(2026, 10, 20), date(2026, 10, 24))
        self.assertEqual(first, "sent")
        self.assertEqual(second, "already sent")
        send.assert_called_once()
        self.assertEqual(ReportDelivery.query.one().status, "sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
