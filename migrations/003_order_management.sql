-- Group 3: Order Management schema upgrade for the existing PostgreSQL database.
-- Run this file once with psql before starting the upgraded application.
-- The transaction guarantees that a failed preflight check leaves the schema unchanged.

BEGIN;
SET LOCAL TIME ZONE 'UTC';

-- Abort instead of silently rewriting invalid legacy quantities. These rows need
-- a deliberate business decision before non-negative/positive constraints can apply.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM products WHERE quantity < 0) THEN
        RAISE EXCEPTION 'Migration stopped: products contains a negative quantity.';
    END IF;

    IF EXISTS (SELECT 1 FROM order_items WHERE quantity <= 0) THEN
        RAISE EXCEPTION 'Migration stopped: order_items contains a non-positive quantity.';
    END IF;
END $$;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS customer_email VARCHAR(254),
    ADD COLUMN IF NOT EXISTS notes TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS inventory_restored BOOLEAN;

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS unit_price NUMERIC(12, 2);

-- Existing orders predate required customer names. A visible neutral placeholder
-- preserves every order while allowing future writes to enforce NOT NULL.
UPDATE orders
SET customer_name = 'Unknown customer'
WHERE customer_name IS NULL OR BTRIM(customer_name) = '';

-- Map legacy demo statuses into the Group 3 state machine before adding its check.
UPDATE orders
SET status = CASE
    WHEN status IN ('Pending', 'Processing', 'Completed', 'Cancelled') THEN status
    WHEN status = 'Shipped' THEN 'Processing'
    WHEN status = 'Delivered' THEN 'Completed'
    ELSE 'Pending'
END;

UPDATE orders
SET order_date = COALESCE(order_date, CURRENT_TIMESTAMP),
    updated_at = COALESCE(updated_at, order_date, CURRENT_TIMESTAMP),
    inventory_restored = CASE
        WHEN status = 'Cancelled' THEN TRUE
        ELSE COALESCE(inventory_restored, FALSE)
    END;

-- Historical prices did not exist before Group 3. The only safe available
-- backfill is the product's current price at migration time; all new orders
-- snapshot the exact price during their atomic creation transaction.
UPDATE order_items AS item
SET unit_price = ROUND(GREATEST(COALESCE(product.price, 0), 0)::NUMERIC, 2)
FROM products AS product
WHERE product.id = item.product_id
  AND item.unit_price IS NULL;

UPDATE order_items
SET unit_price = 0
WHERE unit_price IS NULL;

ALTER TABLE orders
    ALTER COLUMN customer_name SET NOT NULL,
    ALTER COLUMN order_date SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN order_date SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'Pending',
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN updated_at SET NOT NULL,
    ALTER COLUMN inventory_restored SET DEFAULT FALSE,
    ALTER COLUMN inventory_restored SET NOT NULL;

ALTER TABLE order_items
    ALTER COLUMN unit_price SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_products_quantity_nonnegative'
          AND conrelid = 'products'::regclass
    ) THEN
        ALTER TABLE products ADD CONSTRAINT ck_products_quantity_nonnegative CHECK (quantity >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_order_items_quantity_positive'
          AND conrelid = 'order_items'::regclass
    ) THEN
        ALTER TABLE order_items ADD CONSTRAINT ck_order_items_quantity_positive CHECK (quantity > 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_order_items_unit_price_nonnegative'
          AND conrelid = 'order_items'::regclass
    ) THEN
        ALTER TABLE order_items ADD CONSTRAINT ck_order_items_unit_price_nonnegative CHECK (unit_price >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_orders_status_valid'
          AND conrelid = 'orders'::regclass
    ) THEN
        ALTER TABLE orders ADD CONSTRAINT ck_orders_status_valid CHECK (status IN ('Pending', 'Processing', 'Completed', 'Cancelled'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_orders_order_date ON orders (order_date);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status);

COMMIT;

-- Rollback considerations:
-- Do not drop the new columns after Group 3 orders exist: doing so destroys email,
-- notes, price snapshots, and restoration history. Restore a pre-migration database
-- backup together with the pre-Group-3 application if a full rollback is required.
