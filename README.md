# Makhzoon

> **Project status:** Phase 1 (foundation, authentication, authorization, and database reliability) completed and locally validated. Phase 2 business enhancements are planned; this repository does not claim those features are implemented yet.
>
> **Verification:** 34 local tests and 5 PostgreSQL integration tests passed using the isolated test runner. Owner and Staff login and store-scoped access were also checked manually.

## Development milestones

| Milestone | Status | Scope |
| --- | --- | --- |
| Initial Retail Inventory Management System | Completed | Core inventory and product workflows |
| Orders and multi-store reporting | Completed | Transactional orders, per-store stock, reporting |
| Makhzoon Phase 1 | Completed | Owner/Staff authentication, permissions, CSRF, idempotency, UTC/Cairo dates, database migrations and tests |
| Makhzoon Phase 2 | Planned | Returns, stock transfers, barcode lookup, product and reporting improvements |
| Deployment and merchant pilot | Planned | Hosted application, backups, scheduled reports, live operational validation |

**Security note:** Never commit credentials, local `.env` files, or database backups. Previously exposed database and Flask credentials have been rotated. Historical commits may still contain obsolete values; history sanitization is a separate security task.

Makhzoon is a server-rendered Flask application for products, per-store
inventory, orders, audit history, and business reporting backed by PostgreSQL.

## Architecture

The project intentionally remains a straightforward Flask monolith:

- `app.py` initializes Flask and contains routes, validation, authentication,
  authorization, and transactional workflows.
- `models.py` contains the Flask-SQLAlchemy models and relationships.
- `analytics.py` builds reporting metrics and explainable recommendations.
- `reporting.py` renders PDFs and provides explicitly enabled SMTP delivery.
- `report_scheduler.py` is the external scheduled-report entry point.
- Jinja templates and Bootstrap provide the server-rendered interface.
- PostgreSQL is the persistent store and source of truth.

`store_inventory.quantity` is authoritative stock. `products.quantity` is a
synchronized compatibility total. Order lines preserve historical selling
prices and known purchase-cost snapshots.

## Phase 1 security model

- Email/password authentication uses Flask-Login and Werkzeug password hashes.
- Sessions last 12 hours and use HttpOnly, SameSite=Lax cookies. Set
  `SESSION_COOKIE_SECURE=true` whenever HTTPS is used.
- State-changing forms use a session CSRF token.
- Login throttling is database-backed.
- Password-reset links are single-use and expire; only token digests are stored.
- There is no public registration.
- Owners can use all business and administration features.
- Staff can create and view orders only for assigned stores and can move orders
  forward from Pending to Processing to Completed.
- Staff cannot view purchase costs, inventory management, dashboard metrics,
  reports, PDFs, store configuration, user management, or cancellation actions.
- Order submission tokens and a database unique constraint prevent duplicate
  orders and duplicate stock deductions.

## Project layout

```text
.
|-- app.py
|-- models.py
|-- config.py
|-- time_utils.py
|-- analytics.py
|-- reporting.py
|-- report_scheduler.py
|-- migrations/
|   |-- 000_baseline_post_004.sql
|   |-- 003_order_management.sql
|   |-- 004_multistore_reporting.sql
|   `-- 005_authentication_security.sql
|-- scripts/
|-- static/
|-- templates/
|   |-- auth/
|   |-- inventory/
|   |-- orders/
|   |-- products/
|   |-- reports/
|   `-- users/
|-- tests/
|   |-- test_orders.py
|   |-- test_inventory_reporting.py
|   |-- test_security_auth.py
|   |-- test_timezone.py
|   `-- test_postgres_integration.py
|-- .env.example
|-- .gitignore
`-- requirements.txt
```

Local environment files, editor settings, caches, generated reports, SQLite
files, and database backups/exports are excluded from Git.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally. At minimum, provide `DATABASE_URL`, a long random
`SECRET_KEY`, and `BUSINESS_TIMEZONE=Africa/Cairo`. Never commit `.env` or put
credentials in source. `config.py` loads the project-local file for direct
execution, Flask CLI, scripts, and VS Code. PostgreSQL sessions use UTC.

## PostgreSQL initialization and migrations

The baseline is a reviewed, data-free schema representing the application after
migration 004. It contains all post-004 tables, constraints, indexes, and the
inventory synchronization function/trigger.

For a brand-new empty PostgreSQL database, apply only:

```text
000_baseline_post_004.sql
005_authentication_security.sql
future migrations in numeric order
```

Do not apply migrations 003 or 004 after the baseline; their final effects are
already included.

After independently creating and confirming an empty target database, initialize
it with PostgreSQL's client (use a dedicated URL, never the working database URL):

```powershell
$env:NEW_DATABASE_URL = "postgresql://USER:PASSWORD@localhost:5432/makhzoon_new"
psql $env:NEW_DATABASE_URL -v ON_ERROR_STOP=1 -f migrations/000_baseline_post_004.sql
psql $env:NEW_DATABASE_URL -v ON_ERROR_STOP=1 -f migrations/005_authentication_security.sql
Remove-Item Env:NEW_DATABASE_URL
```

The baseline is intentionally strict and fails if its objects already exist.

For a legacy database that predates migration 003, use:

```text
003_order_management.sql
004_multistore_reporting.sql
005_authentication_security.sql
future migrations in numeric order
```

For an existing database already at migration 004, apply migration 005 next.
Always back up, confirm the target, and test against a copy first. The application
never runs migrations automatically. A read-only target/schema check is available:

```powershell
python scripts/verify_postgres.py
```

The application never applies migration 005 automatically. Confirm the target
schema and maintain a tested backup before applying migrations in any new
environment. Legacy users remain valid but receive the least-privileged Staff
role and a nullable email; designate a real Owner explicitly with the CLI.

## First Owner and administrative recovery

After the schema includes migration 005:

```powershell
$env:FLASK_APP = "app.py"
flask create-owner
```

The command prompts for email, username, and a hidden confirmed password. It does
not create a default password. If email recovery is unavailable:

```powershell
flask reset-owner-password
```

That command also uses a hidden password prompt and is not exposed as a web route.

## Start the application

```powershell
python app.py
```

or:

```powershell
$env:FLASK_APP = "app.py"
flask run
```

Open `http://127.0.0.1:5000`. Unauthenticated users are redirected to `/login`.
Owners land on the dashboard and Staff land on the order workspace.

## Password-reset email

Authentication email is disabled unless `ENABLE_AUTH_EMAIL=true`. Configure
`SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, optional credentials, and TLS settings in
the local environment. Set `PUBLIC_BASE_URL` to the trusted application origin
(for example, `https://inventory.example.com`) so reset links never depend on an
untrusted request Host header. Automated tests mock delivery and never send real email.
Scheduled report email remains independently controlled by
`ENABLE_REPORT_EMAIL`.

## Tests

SQLite regression, security, CSRF, role, and timezone tests:

```powershell
python -m pytest -q --ignore=tests/test_postgres_integration.py
```

The PostgreSQL concurrency suite is separately opt-in and must use a dedicated
disposable database. It refuses to run unless the database name contains `test`,
the target differs from `DATABASE_URL`, and explicit opt-in is present:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg2://USER:PASSWORD@localhost:5432/makhzoon_test"
$env:RUN_POSTGRES_INTEGRATION = "1"
python -m unittest discover -s tests -p "test_postgres_integration.py" -v
```

The suite creates and drops only a randomly named `makhzoon_it_<uuid>` schema
inside that verified test database. Never point `TEST_DATABASE_URL` at the
working database.

Do not run plain `python -m pytest -q` with PostgreSQL integration enabled.
Pytest can import the local test modules first, while the PostgreSQL suite
intentionally requires a clean `app`/`models`/`config` module registry. Use the
isolated combined runner instead; it launches both suites in separate Python
processes and returns a single combined exit status:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg2://USER:PASSWORD@localhost:5432/makhzoon_test"
$env:RUN_POSTGRES_INTEGRATION = "1"
python scripts/run_tests.py
```

## Reports and scheduler

Owners can use `/reports`; PDFs are produced in memory. Email delivery stays
disabled unless explicitly configured. Run the scheduler once per day through
Windows Task Scheduler or cron:

```powershell
python report_scheduler.py
```

It calculates due dates in `Africa/Cairo` and queries UTC database boundaries.
Use `--force` only for an intentional configured delivery test.

## Known data limitations

- Historical timestamps are not rewritten; Phase 1 establishes the UTC
  convention for new writes and converts business boundaries/display to Cairo.
- Legacy orders can have unknown store, price-tier, cost, and user attribution.
- Gross profit is reported only where a reliable cost snapshot exists.
- Employee performance remains hidden until attribution data is sufficiently
  complete for meaningful reporting.
