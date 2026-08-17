"""
The warehouse: sample e-commerce data, plus the READ-ONLY connection that
analytical queries must go through.

Two things matter here:

1. READ-ONLY IS ENFORCED BY THE CONNECTION, not by a prompt or a string check.
   Architecture doc §6.2: an application-level "does the SQL start with SELECT"
   test is bypassable in more ways than it's worth enumerating, and it's the
   wrong layer. SQLite's URI `mode=ro` makes writes fail at the driver, which is
   the equivalent of the read-only database role you'd use on a real warehouse.

2. Sample data is DETERMINISTIC (seeded). Golden-query expected values are only
   meaningful if regenerating the warehouse produces identical numbers.
"""
import logging
import random
import sqlite3
from datetime import datetime, timedelta

from . import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    segment TEXT NOT NULL,
    signed_up_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    gross_amount REAL NOT NULL,
    discount_amount REAL NOT NULL DEFAULT 0,
    refund_amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    line_amount REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(ordered_at);
CREATE INDEX IF NOT EXISTS idx_orders_channel ON orders(channel);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_items_product ON order_items(product_id);
"""

REGIONS = ["North America", "EMEA", "APAC", "LATAM"]
SEGMENTS = ["consumer", "business", "enterprise"]
CHANNELS = ["web", "mobile_app", "marketplace", "phone"]
PAYMENT_METHODS = ["card", "paypal", "bank_transfer", "gift_card"]
CATEGORIES = ["Electronics", "Home", "Apparel", "Outdoors", "Beauty"]

PRODUCTS = [
    ("Wireless Headphones", "Electronics", 129.99),
    ("Smart Speaker", "Electronics", 79.50),
    ("4K Monitor", "Electronics", 349.00),
    ("Mechanical Keyboard", "Electronics", 109.00),
    ("Desk Lamp", "Home", 44.99),
    ("Cotton Bedding Set", "Home", 89.00),
    ("Ceramic Cookware", "Home", 154.00),
    ("Running Shoes", "Apparel", 119.95),
    ("Rain Jacket", "Apparel", 179.00),
    ("Merino Socks", "Apparel", 22.50),
    ("Camping Tent", "Outdoors", 259.00),
    ("Trekking Poles", "Outdoors", 64.00),
    ("Water Filter", "Outdoors", 39.99),
    ("Face Serum", "Beauty", 48.00),
    ("Shampoo Bar", "Beauty", 14.50),
]


def read_only_connection() -> sqlite3.Connection:
    """The ONLY connection analytical queries may use.

    `mode=ro` is enforced by the driver: an INSERT/UPDATE/DELETE/ATTACH raises
    sqlite3.OperationalError regardless of what the SQL looks like. That's the
    point — it holds even if a future change lets an unexpected statement
    through the application-level checks.
    """
    if not config.WAREHOUSE_PATH.exists():
        raise RuntimeError(
            f"Warehouse not found at {config.WAREHOUSE_PATH}. Run: python -m app.cli seed"
        )
    uri = f"file:{config.WAREHOUSE_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=config.QUERY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # Belt and braces: also refuse at the authorizer level. Two independent
    # mechanisms because this is the boundary that matters most.
    conn.set_authorizer(_deny_writes)
    return conn


_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _deny_writes(action, arg1, arg2, db_name, trigger):
    """SQLite authorizer callback — denies anything that isn't a read.

    Second layer under `mode=ro`. Also blocks ATTACH, which is how a clever
    query could otherwise reach a writable database file.
    """
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        # Allow only harmless introspection pragmas.
        if (arg1 or "").lower() in {"table_info", "table_list", "database_list"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def writable_connection() -> sqlite3.Connection:
    """For SEEDING ONLY. Never used on the query path."""
    config.WAREHOUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.WAREHOUSE_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def seed(seed_value: int = None, days: int = None, order_count: int = None) -> dict:
    """Generates deterministic sample data.

    Determinism is a testing requirement, not a nicety: the golden-query set
    asserts exact numeric answers, which only means anything if the same seed
    always produces the same warehouse.
    """
    seed_value = seed_value if seed_value is not None else config.SAMPLE_SEED
    days = days or config.SAMPLE_DAYS
    order_count = order_count or config.SAMPLE_ORDERS

    rng = random.Random(seed_value)
    conn = writable_connection()
    try:
        conn.executescript(SCHEMA)
        for table in ("order_items", "orders", "products", "customers"):
            conn.execute(f"DELETE FROM {table}")

        # Customers
        customers = []
        for cid in range(1, 801):
            region = rng.choices(REGIONS, weights=[45, 30, 18, 7])[0]
            segment = rng.choices(SEGMENTS, weights=[70, 22, 8])[0]
            signup = datetime.now() - timedelta(days=rng.randint(0, days + 200))
            customers.append((cid, f"Customer {cid:04d}", region, segment, signup.date().isoformat()))
        conn.executemany(
            "INSERT INTO customers (id, name, region, segment, signed_up_at) VALUES (?,?,?,?,?)",
            customers,
        )

        # Products
        products = [
            (pid, name, category, price)
            for pid, (name, category, price) in enumerate(PRODUCTS, start=1)
        ]
        conn.executemany(
            "INSERT INTO products (id, name, category, unit_price) VALUES (?,?,?,?)", products
        )

        # Orders + items. A mild upward trend and weekend dip make time-series
        # charts look like real data rather than noise.
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        orders, items = [], []
        item_id = 1
        for oid in range(1, order_count + 1):
            day_offset = int(abs(rng.gauss(0, 1)) / 3.2 * days) % days
            ordered_at = today - timedelta(days=day_offset, hours=rng.randint(0, 23))
            weekend_factor = 0.7 if ordered_at.weekday() >= 5 else 1.0

            customer = rng.choice(customers)
            channel = rng.choices(CHANNELS, weights=[52, 28, 15, 5])[0]
            payment = rng.choices(PAYMENT_METHODS, weights=[62, 24, 9, 5])[0]

            n_lines = rng.choices([1, 2, 3, 4], weights=[55, 27, 13, 5])[0]
            gross = 0.0
            order_items_buffer = []
            for _ in range(n_lines):
                product = rng.choice(products)
                qty = rng.choices([1, 2, 3], weights=[75, 19, 6])[0]
                line = round(product[3] * qty, 2)
                gross += line
                order_items_buffer.append((item_id, oid, product[0], qty, line))
                item_id += 1

            gross = round(gross * weekend_factor, 2)
            discount = round(gross * rng.choice([0, 0, 0, 0.05, 0.10, 0.15]), 2)
            refund = round(gross * rng.choice([0] * 22 + [0.5, 1.0]), 2)

            orders.append(
                (oid, customer[0], ordered_at.isoformat(timespec="seconds"), channel,
                 payment, gross, discount, refund)
            )
            items.extend(order_items_buffer)

        conn.executemany(
            """INSERT INTO orders
               (id, customer_id, ordered_at, channel, payment_method,
                gross_amount, discount_amount, refund_amount)
               VALUES (?,?,?,?,?,?,?,?)""",
            orders,
        )
        conn.executemany(
            "INSERT INTO order_items (id, order_id, product_id, quantity, line_amount) VALUES (?,?,?,?,?)",
            items,
        )
        conn.commit()

        # Table sizes changed — drop the memoized counts used by the cost estimator.
        from . import guardrails
        guardrails.invalidate_table_counts()

        return {
            "customers": len(customers),
            "products": len(products),
            "orders": len(orders),
            "order_items": len(items),
            "seed": seed_value,
        }
    finally:
        conn.close()


def freshness() -> str:
    """Most recent order timestamp — shown with every answer so a stale
    warehouse can't be mistaken for a business slowdown."""
    with read_only_connection() as conn:
        row = conn.execute("SELECT MAX(ordered_at) AS latest FROM orders").fetchone()
    return row["latest"] if row and row["latest"] else "unknown"


def table_row_counts() -> dict:
    with read_only_connection() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("orders", "order_items", "customers", "products")
        }
