-- Phase 1 authentication, authorization, audit attribution and idempotency.
-- PostgreSQL only. Apply after migration 004, or after the clean post-004
-- baseline. This migration is additive and preserves all legacy business rows.
-- New user emails and order attribution remain nullable so the application can
-- explicitly associate legacy records instead of inventing identity data.

BEGIN;
SET LOCAL TIME ZONE 'UTC';

-- Pin the existing trigger function to the schema path used for this migration
-- instead of inheriting a caller-controlled path at execution time.ش
ALTER FUNCTION sync_product_quantity_from_stores()
    SET search_path FROM CURRENT;

-- Preserve historical values while making every database-side default an
-- explicit naive-UTC value independent of the server/session timezone.
ALTER TABLE stores
    ALTER COLUMN created_at SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
ALTER TABLE products
    ALTER COLUMN date_added SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
ALTER TABLE store_inventory
    ALTER COLUMN updated_at SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
ALTER TABLE orders
    ALTER COLUMN order_date SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    ALTER COLUMN updated_at SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
ALTER TABLE product_history
    ALTER COLUMN created_at SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
ALTER TABLE report_deliveries
    ALTER COLUMN created_at SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC');

-- Authentication metadata. Legacy accounts receive the least-privileged role;
-- an owner must be designated explicitly through the administrative CLI.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email VARCHAR(254),
    ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'staff',
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMP WITHOUT TIME ZONE;

UPDATE users
SET date_created = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
WHERE date_created IS NULL;

ALTER TABLE users
    ALTER COLUMN date_created SET DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    ALTER COLUMN date_created SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_role_valid'
          AND conrelid = 'users'::regclass
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_role_valid
            CHECK (role IN ('owner', 'staff'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_email_normalized'
          AND conrelid = 'users'::regclass
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_email_normalized
            CHECK (email IS NULL OR email = LOWER(BTRIM(email)));
    END IF;
END $$;

-- The functional index provides database-enforced case-insensitive uniqueness
-- without requiring the citext extension. PostgreSQL permits multiple NULLs,
-- which is required for unidentified legacy users.
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_normalized
    ON users (LOWER(email))
    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_users_role ON users(role);
CREATE INDEX IF NOT EXISTS ix_users_is_active ON users(is_active);

-- Owners can access every store. Only staff assignments are stored here.
CREATE TABLE IF NOT EXISTS user_store_assignments (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    CONSTRAINT pk_user_store_assignments PRIMARY KEY (user_id, store_id)
);

CREATE INDEX IF NOT EXISTS ix_user_store_assignments_store_id
    ON user_store_assignments(store_id);

-- Attribution is nullable for legacy orders. User deletion must not delete or
-- invalidate order history, so attribution uses ON DELETE SET NULL. Only a
-- SHA-256 digest of the browser submission token is persisted.
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS status_changed_by_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS submission_token_hash CHAR(64);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_orders_created_by_user_id'
          AND conrelid = 'orders'::regclass
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT fk_orders_created_by_user_id
            FOREIGN KEY (created_by_user_id)
            REFERENCES users(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_orders_status_changed_by_user_id'
          AND conrelid = 'orders'::regclass
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT fk_orders_status_changed_by_user_id
            FOREIGN KEY (status_changed_by_user_id)
            REFERENCES users(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_orders_submission_token_hash'
          AND conrelid = 'orders'::regclass
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT uq_orders_submission_token_hash
            UNIQUE (submission_token_hash);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_orders_submission_token_hash_length'
          AND conrelid = 'orders'::regclass
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT ck_orders_submission_token_hash_length
            CHECK (
                submission_token_hash IS NULL
                OR CHAR_LENGTH(submission_token_hash) = 64
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_orders_created_by_user_id
    ON orders(created_by_user_id);
CREATE INDEX IF NOT EXISTS ix_orders_status_changed_by_user_id
    ON orders(status_changed_by_user_id);

-- Store only a one-way digest of each password-reset token. A token is consumed
-- by setting used_at in the same transaction that changes the password.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    used_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT ck_password_reset_token_hash_length
        CHECK (CHAR_LENGTH(token_hash) = 64),
    CONSTRAINT ck_password_reset_token_expiry
        CHECK (expires_at > created_at),
    CONSTRAINT ck_password_reset_token_use_time
        CHECK (used_at IS NULL OR used_at >= created_at)
);

CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id
    ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_expires_at
    ON password_reset_tokens(expires_at);

-- Each login attempt stores only keyed SHA-256 digests of the normalized login
-- identity and remote address. The application counts recent failed rows to
-- enforce rate limits without persisting plaintext identifiers in this table.
CREATE TABLE IF NOT EXISTS login_attempts (
    id SERIAL PRIMARY KEY,
    identity_hash CHAR(64) NOT NULL,
    remote_addr_hash CHAR(64) NOT NULL,
    attempted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    was_successful BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_login_attempt_identity_hash_length
        CHECK (CHAR_LENGTH(identity_hash) = 64),
    CONSTRAINT ck_login_attempt_remote_hash_length
        CHECK (CHAR_LENGTH(remote_addr_hash) = 64)
);

CREATE INDEX IF NOT EXISTS ix_login_attempts_identity_time
    ON login_attempts(identity_hash, attempted_at);
CREATE INDEX IF NOT EXISTS ix_login_attempts_remote_time
    ON login_attempts(remote_addr_hash, attempted_at);

COMMIT;
