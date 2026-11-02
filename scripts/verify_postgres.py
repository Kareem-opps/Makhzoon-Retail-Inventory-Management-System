"""Read-only verification of the configured PostgreSQL target and schema."""

from pathlib import Path
import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app, db  # noqa: E402


REQUIRED_COLUMNS = {
    "users": {
        "email", "role", "is_active", "last_login_at", "password_changed_at"
    },
    "products": {
        "purchase_price", "wholesale_price", "semi_wholesale_price", "retail_price"
    },
    "orders": {
        "store_id", "employee_reference", "submission_token_hash",
        "created_by_user_id", "status_changed_by_user_id",
    },
    "order_items": {"price_tier", "purchase_unit_cost"},
}
REQUIRED_TABLES = {
    "users", "stores", "products", "store_inventory", "orders", "order_items",
    "product_history", "report_deliveries", "user_store_assignments",
    "password_reset_tokens", "login_attempts",
}


def main():
    with app.app_context():
        configured = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
        print(f"Configured target: {configured.render_as_string(hide_password=True)}")
        with db.engine.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_database(), current_user, inet_server_port()"
            )).one()
            print(
                f"Connected: database={identity[0]}, user={identity[1]}, "
                f"port={identity[2]}"
            )
            product_count = connection.execute(text("SELECT COUNT(*) FROM products")).scalar_one()
            order_count = connection.execute(text("SELECT COUNT(*) FROM orders")).scalar_one()
            print(f"Existing records preserved: products={product_count}, orders={order_count}")

            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            missing_tables = sorted(REQUIRED_TABLES - tables)
            missing_columns = {}
            for table, required in REQUIRED_COLUMNS.items():
                if table not in tables:
                    continue
                present = {column["name"] for column in inspector.get_columns(table)}
                missing = sorted(required - present)
                if missing:
                    missing_columns[table] = missing

            print(f"Phase 1 tables present: {not missing_tables}")
            print(f"Phase 1 columns present: {not missing_columns}")
            if missing_tables:
                print(f"Missing tables: {', '.join(missing_tables)}")
            for table, columns in missing_columns.items():
                print(f"Missing columns in {table}: {', '.join(columns)}")

            trigger_present = connection.execute(text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_sync_product_quantity'
                      AND tgrelid = 'store_inventory'::regclass
                      AND NOT tgisinternal
                )
                """
            )).scalar_one()
            print(f"Stock synchronization trigger present: {trigger_present}")
            timezone_name = connection.execute(text("SHOW TIME ZONE")).scalar_one()
            print(f"Database session timezone is UTC: {timezone_name.upper() == 'UTC'}")

            if missing_tables or missing_columns or not trigger_present or timezone_name.upper() != "UTC":
                raise SystemExit(2)


if __name__ == "__main__":
    main()
