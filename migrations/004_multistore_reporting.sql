-- Six-phase inventory/reporting upgrade. PostgreSQL only.
BEGIN;
SET LOCAL TIME ZONE 'UTC';

CREATE TABLE IF NOT EXISTS stores (
    id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS purchase_price NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS wholesale_price NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS semi_wholesale_price NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS retail_price NUMERIC(12,2);

-- The known old selling price may seed selling tiers. Purchase cost is unknown
-- and remains zero with no purchase event, so it is never counted as acquired cost.
UPDATE products SET
    purchase_price = COALESCE(purchase_price, 0),
    wholesale_price = COALESCE(wholesale_price, GREATEST(COALESCE(price, 0), 0)),
    semi_wholesale_price = COALESCE(semi_wholesale_price, GREATEST(COALESCE(price, 0), 0)),
    retail_price = COALESCE(retail_price, GREATEST(COALESCE(price, 0), 0));

ALTER TABLE products
    ALTER COLUMN price TYPE NUMERIC(12,2) USING ROUND(COALESCE(price, 0)::NUMERIC, 2),
    ALTER COLUMN purchase_price SET DEFAULT 0, ALTER COLUMN purchase_price SET NOT NULL,
    ALTER COLUMN wholesale_price SET DEFAULT 0, ALTER COLUMN wholesale_price SET NOT NULL,
    ALTER COLUMN semi_wholesale_price SET DEFAULT 0, ALTER COLUMN semi_wholesale_price SET NOT NULL,
    ALTER COLUMN retail_price SET DEFAULT 0, ALTER COLUMN retail_price SET NOT NULL;

CREATE TABLE IF NOT EXISTS store_inventory (
    id SERIAL PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_store_inventory UNIQUE (store_id, product_id)
);

-- This explicit compatibility location preserves totals without inventing an
-- original store assignment.
INSERT INTO stores (name) VALUES ('Legacy / Unassigned') ON CONFLICT (name) DO NOTHING;
INSERT INTO store_inventory (store_id, product_id, quantity)
SELECT s.id, p.id, GREATEST(p.quantity, 0) FROM products p CROSS JOIN stores s
WHERE s.name = 'Legacy / Unassigned' ON CONFLICT (store_id, product_id) DO NOTHING;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id),
    ADD COLUMN IF NOT EXISTS employee_reference VARCHAR(100);
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS price_tier VARCHAR(24),
    ADD COLUMN IF NOT EXISTS purchase_unit_cost NUMERIC(12,2);

CREATE TABLE IF NOT EXISTS product_history (
    id SERIAL PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
    store_id INTEGER REFERENCES stores(id), order_id INTEGER REFERENCES orders(id),
    event_type VARCHAR(40) NOT NULL, quantity_change INTEGER, old_quantity INTEGER, new_quantity INTEGER,
    purchase_unit_cost NUMERIC(12,2), price_field VARCHAR(40), old_price NUMERIC(12,2), new_price NUMERIC(12,2),
    details TEXT, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS report_deliveries (
    id SERIAL PRIMARY KEY, schedule_type VARCHAR(20) NOT NULL, period_start DATE NOT NULL, period_end DATE NOT NULL,
    recipient VARCHAR(254) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT, sent_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_report_delivery_period UNIQUE (schedule_type, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS ix_store_inventory_store_id ON store_inventory(store_id);
CREATE INDEX IF NOT EXISTS ix_store_inventory_product_id ON store_inventory(product_id);
CREATE INDEX IF NOT EXISTS ix_orders_store_id ON orders(store_id);
CREATE INDEX IF NOT EXISTS ix_product_history_product_id ON product_history(product_id);
CREATE INDEX IF NOT EXISTS ix_product_history_created_at ON product_history(created_at);

-- Enforce the compatibility cache even for trusted direct SQL writes. The
-- application also synchronizes inside its transaction so ORM objects agree.
CREATE OR REPLACE FUNCTION sync_product_quantity_from_stores() RETURNS TRIGGER AS $$
DECLARE target_product_id INTEGER;
BEGIN
    target_product_id := COALESCE(NEW.product_id, OLD.product_id);
    UPDATE products SET quantity = (
        SELECT COALESCE(SUM(quantity), 0)
        FROM store_inventory WHERE product_id = target_product_id
    ) WHERE id = target_product_id;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_product_quantity ON store_inventory;
CREATE TRIGGER trg_sync_product_quantity
AFTER INSERT OR UPDATE OF quantity OR DELETE ON store_inventory
FOR EACH ROW EXECUTE FUNCTION sync_product_quantity_from_stores();

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_products_purchase_price_nonnegative') THEN ALTER TABLE products ADD CONSTRAINT ck_products_purchase_price_nonnegative CHECK (purchase_price >= 0); END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_products_wholesale_price_nonnegative') THEN ALTER TABLE products ADD CONSTRAINT ck_products_wholesale_price_nonnegative CHECK (wholesale_price >= 0); END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_products_semi_price_nonnegative') THEN ALTER TABLE products ADD CONSTRAINT ck_products_semi_price_nonnegative CHECK (semi_wholesale_price >= 0); END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_products_retail_price_nonnegative') THEN ALTER TABLE products ADD CONSTRAINT ck_products_retail_price_nonnegative CHECK (retail_price >= 0); END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_order_items_price_tier_valid') THEN ALTER TABLE order_items ADD CONSTRAINT ck_order_items_price_tier_valid CHECK (price_tier IS NULL OR price_tier IN ('wholesale','semi_wholesale','retail')); END IF;
END $$;

COMMIT;

-- Existing orders intentionally retain NULL store_id, price_tier and purchase_unit_cost.
