"""Business-date regression tests around Cairo midnight."""

import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from config import Config

Config.SQLALCHEMY_DATABASE_URI = "sqlite://"
Config.SECRET_KEY = "timezone-test-secret-not-for-production"
Config.TESTING = True
Config.CSRF_ENABLED = False

from analytics import build_report  # noqa: E402
from app import app, db  # noqa: E402
from models import Order, OrderItem, Product, Store, StoreInventory, User  # noqa: E402
from report_scheduler import run_due  # noqa: E402
from time_utils import local_date_range_to_utc, utc_to_local  # noqa: E402


class CairoTimezoneTestCase(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        app.config.update(
            TESTING=True,
            CSRF_ENABLED=False,
            BUSINESS_TIMEZONE="Africa/Cairo",
        )
        db.create_all()
        self.owner = User(
            username="owner", email="owner@example.test", role="owner", is_active=True
        )
        self.owner.set_password("test-owner-password")
        self.store = Store(name="Cairo Store")
        self.product = Product(
            name="Clock", category="Accessories", supplier="Supplier",
            quantity=10, price=Decimal("100"), purchase_price=Decimal("50"),
            wholesale_price=Decimal("80"), semi_wholesale_price=Decimal("90"),
            retail_price=Decimal("100"), reorder_level=2,
        )
        db.session.add_all([self.owner, self.store, self.product])
        db.session.flush()
        db.session.add(StoreInventory(
            store=self.store, product=self.product, quantity=10
        ))
        db.session.commit()
        self.client = app.test_client()
        self.client.post("/login", data={
            "email": self.owner.email, "password": "test-owner-password"
        })

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_order(self, customer_name, order_date):
        order = Order(
            customer_name=customer_name, store=self.store, order_date=order_date,
            updated_at=order_date, created_by_user=self.owner,
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(
            order=order, product=self.product, quantity=1, price_tier="retail",
            unit_price=Decimal("100"), purchase_unit_cost=Decimal("50"),
        ))
        db.session.commit()
        return order

    def test_cairo_calendar_filter_and_display_cross_utc_date(self):
        start, end = local_date_range_to_utc(
            date(2026, 11, 1), date(2026, 11, 2), "Africa/Cairo"
        )
        self.assertEqual(utc_to_local(start, "Africa/Cairo").date(), date(2026, 11, 1))
        self.assertEqual(utc_to_local(end, "Africa/Cairo").date(), date(2026, 11, 2))

        before = self.add_order("Before Cairo midnight", start - timedelta(minutes=1))
        inside = self.add_order("After Cairo midnight", start + timedelta(minutes=1))

        response = self.client.get("/orders?date_from=2026-11-01&date_to=2026-11-01")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(before.customer_name, body)
        self.assertIn(inside.customer_name, body)
        self.assertIn("01 Nov 2026, 00:01", body)

    def test_report_daily_group_and_dashboard_yesterday_use_cairo_dates(self):
        start, end = local_date_range_to_utc(
            date(2026, 11, 1), date(2026, 11, 2), "Africa/Cairo"
        )
        previous = self.add_order("Previous local day", start - timedelta(minutes=1))
        cairo_first = self.add_order("Cairo first minute", start + timedelta(minutes=1))

        report = build_report(
            start, end, "Africa/Cairo", date(2026, 11, 1), date(2026, 11, 2)
        )
        self.assertEqual(report["summary"]["orders"], 1)
        self.assertEqual(report["daily"][0]["date"], date(2026, 11, 1))

        with patch("app.business_local_today", return_value=date(2026, 11, 2)):
            body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn(f"#{cairo_first.id}", body)
        self.assertNotIn(f"#{previous.id}", body)
        self.assertIn("00:01", body)

    def test_scheduler_uses_cairo_today_for_completed_periods(self):
        with patch("report_scheduler.local_today", return_value=date(2026, 11, 1)), patch(
            "report_scheduler.deliver", return_value="sent"
        ) as deliver:
            results = run_due(force=True)

        self.assertEqual(results, [("five_day", "sent"), ("monthly", "sent")])
        self.assertEqual(
            deliver.call_args_list[0].args,
            ("five_day", date(2026, 10, 27), date(2026, 10, 31)),
        )
        self.assertEqual(
            deliver.call_args_list[1].args,
            ("monthly", date(2026, 10, 1), date(2026, 10, 31)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
