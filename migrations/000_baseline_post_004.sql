-- Makhzoon clean PostgreSQL baseline: schema state after migration 004.
--
-- Use this file only for a brand-new, empty PostgreSQL database. It contains
-- no business data and intentionally does not create the legacy compatibility
-- store used while upgrading existing installations.
--
-- Fresh database order:
--   1. 000_baseline_post_004.sql
--   2. 005_authentication_security.sql
--   3. Future migrations in numeric order
--
-- Existing legacy database order:
--   1. 003_order_management.sql (when not already applied)
--   2. 004_multistore_reporting.sql (when not already applied)
--   3. 005_authentication_security.sql
--
-- Never apply migrations 003 or 004 after this baseline: their final schema
-- is already represented here. This script is intentionally strict rather
-- than using IF NOT EXISTS so an accidental run against a populated database
-- fails instead of silently producing a partially initialized schema.

BEGIN;
SET LOCAL TIME ZONE 'UTC';

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(80) NOT NULL UNIQUE,
    password_hash VARCHAR(200) NOT NULL,
    date_created TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
);

CREATE TABLE stores (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
);

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    category VARCHAR(100) NOT NULL,
    supplier VARCHAR(100) NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    purchase_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    wholesale_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    semi_wholesale_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    retail_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    reorder_level INTEGER NOT NULL DEFAULT 5,
    date_added TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    CONSTRAINT ck_products_quantity_nonnegative CHECK (quantity >= 0),
    CONSTRAINT ck_products_purchase_price_nonnegative CHECK (purchase_price >= 0),
    CONSTRAINT ck_products_wholesale_price_nonnegative CHECK (wholesale_price >= 0),
    CONSTRAINT ck_products_semi_price_nonnegative CHECK (semi_wholesale_price >= 0),
    CONSTRAINT ck_products_retail_price_nonnegative CHECK (retail_price >= 0)
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    customer_phone VARCHAR(20),
    customer_email VARCHAR(254),
    notes TEXT,
    store_id INTEGER REFERENCES stores(id),
    employee_reference VARCHAR(100),
    order_date TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    status VARCHAR(20) NOT NULL DEFAULT 'Pending',
    inventory_restored BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_orders_status_valid
        CHECK (status IN ('Pending', 'Processing', 'Completed', 'Cancelled'))
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL DEFAULT 1,
    price_tier VARCHAR(24),
    unit_price NUMERIC(12, 2) NOT NULL,
    purchase_unit_cost NUMERIC(12, 2),
    CONSTRAINT ck_order_items_quantity_positive CHECK (quantity > 0),
    CONSTRAINT ck_order_items_unit_price_nonnegative CHECK (unit_price >= 0),
    CONSTRAINT ck_order_items_price_tier_valid
        CHECK (
            price_tier IS NULL
            OR price_tier IN ('wholesale', 'semi_wholesale', 'retail')
        )
);

CREATE TABLE store_inventory (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    CONSTRAINT uq_store_inventory UNIQUE (store_id, product_id),
    CONSTRAINT ck_store_inventory_nonnegative CHECK (quantity >= 0)
);

CREATE TABLE product_history (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    store_id INTEGER REFERENCES stores(id),
    order_id INTEGER REFERENCES orders(id),
    event_type VARCHAR(40) NOT NULL,
    quantity_change INTEGER,
    old_quantity INTEGER,
    new_quantity INTEGER,
    purchase_unit_cost NUMERIC(12, 2),
    price_field VARCHAR(40),
    old_price NUMERIC(12, 2),
    new_price NUMERIC(12, 2),
    details TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
);

CREATE TABLE report_deliveries (
    id SERIAL PRIMARY KEY,
    schedule_type VARCHAR(20) NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    recipient VARCHAR(254) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    sent_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    CONSTRAINT uq_report_delivery_period
        UNIQUE (schedule_type, period_start, period_end)
);

-- Explicit indexes mirror every index declared by the SQLAlchemy models.
CREATE INDEX ix_orders_store_id ON orders(store_id);
CREATE INDEX ix_orders_order_date ON orders(order_date);
CREATE INDEX ix_orders_status ON orders(status);
CREATE INDEX ix_order_items_order_id ON order_items(order_id);
CREATE INDEX ix_order_items_product_id ON order_items(product_id);
CREATE INDEX ix_store_inventory_store_id ON store_inventory(store_id);
CREATE INDEX ix_store_inventory_product_id ON store_inventory(product_id);
CREATE INDEX ix_product_history_product_id ON product_history(product_id);
CREATE INDEX ix_product_history_store_id ON product_history(store_id);
CREATE INDEX ix_product_history_order_id ON product_history(order_id);
CREATE INDEX ix_product_history_event_type ON product_history(event_type);
CREATE INDEX ix_product_history_created_at ON product_history(created_at);

-- StoreInventory.quantity is authoritative. This trigger maintains the legacy
-- products.quantity aggregate for direct SQL writes as well as ORM writes.
CREATE FUNCTION sync_product_quantity_from_stores()
RETURNS TRIGGER AS $$
DECLARE
    target_product_id INTEGER;
BEGIN
    target_product_id := COALESCE(NEW.product_id, OLD.product_id);

    UPDATE products
    SET quantity = (
        SELECT COALESCE(SUM(quantity), 0)
        FROM store_inventory
        WHERE product_id = target_product_id
    )
    WHERE id = target_product_id;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql
SET search_path FROM CURRENT;

CREATE TRIGGER trg_sync_product_quantity
AFTER INSERT OR UPDATE OF quantity OR DELETE ON store_inventory
FOR EACH ROW
EXECUTE FUNCTION sync_product_quantity_from_stores();

COMMIT;
