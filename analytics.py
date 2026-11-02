"""SQL-backed business analytics and explainable recommendation rules."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func

from models import db, Order, OrderItem, Product, Store, StoreInventory
from time_utils import local_date_range_to_utc, utc_to_local_date

ZERO = Decimal("0.00")


def _sales_rows(start, end):
    return (
        db.session.query(OrderItem, Order, Product, Store)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .outerjoin(Store, Store.id == Order.store_id)
        .filter(Order.status != "Cancelled", Order.order_date >= start, Order.order_date < end)
        .all()
    )


def _summary(rows):
    revenue = sum((item.subtotal for item, *_ in rows), ZERO)
    units = sum(item.quantity for item, *_ in rows)
    order_ids = {order.id for _, order, *_ in rows}
    known_profit = sum(
        (item.gross_profit for item, *_ in rows if item.gross_profit is not None), ZERO
    )
    known_revenue = sum(
        (item.subtotal for item, *_ in rows if item.purchase_unit_cost is not None), ZERO
    )
    return {
        "revenue": revenue,
        "units": units,
        "orders": len(order_ids),
        "average_order_value": revenue / len(order_ids) if order_ids else ZERO,
        "known_profit": known_profit,
        "profit_coverage_revenue": known_revenue,
        "margin_percent": (known_profit / known_revenue * 100) if known_revenue else None,
    }


def build_report(
    start,
    end,
    timezone_name="Africa/Cairo",
    period_start_date=None,
    period_end_date_exclusive=None,
):
    """Return period metrics and rules without presenting unknown costs as facts."""
    rows = _sales_rows(start, end)
    period_start_date = period_start_date or utc_to_local_date(start, timezone_name)
    period_end_date_exclusive = period_end_date_exclusive or utc_to_local_date(end, timezone_name)
    days = max(1, (period_end_date_exclusive - period_start_date).days)
    previous_start_date = period_start_date - timedelta(days=days)
    previous_start, _ = local_date_range_to_utc(
        previous_start_date, period_start_date, timezone_name
    )
    previous_rows = _sales_rows(previous_start, start)
    summary = _summary(rows)
    previous = _summary(previous_rows)

    by_product = defaultdict(lambda: {"units": 0, "revenue": ZERO})
    by_store = defaultdict(lambda: {"units": 0, "revenue": ZERO})
    by_tier = defaultdict(lambda: {"units": 0, "revenue": ZERO})
    daily = defaultdict(lambda: ZERO)
    previous_product_units = defaultdict(int)

    for item, order, product, store in rows:
        key = (product.id, product.name)
        by_product[key]["units"] += item.quantity
        by_product[key]["revenue"] += item.subtotal
        store_key = (store.id, store.name) if store else (None, "Legacy / Unknown")
        by_store[store_key]["units"] += item.quantity
        by_store[store_key]["revenue"] += item.subtotal
        tier = item.price_tier or "legacy_unknown"
        by_tier[tier]["units"] += item.quantity
        by_tier[tier]["revenue"] += item.subtotal
        daily[utc_to_local_date(order.order_date, timezone_name)] += item.subtotal

    for item, _, product, _ in previous_rows:
        previous_product_units[product.id] += item.quantity

    products = Product.query.order_by(Product.name).all()
    inventories = {
        product_id: quantity
        for product_id, quantity in (
            db.session.query(
                StoreInventory.product_id, func.coalesce(func.sum(StoreInventory.quantity), 0)
            ).group_by(StoreInventory.product_id).all()
        )
    }
    product_rows = []
    zero_sales = []
    declining = []
    high_stock_weak = []
    low_stock_demand = []
    for product in products:
        metrics = by_product.get((product.id, product.name), {"units": 0, "revenue": ZERO})
        stock = int(inventories.get(product.id, product.quantity or 0))
        row = {"id": product.id, "name": product.name, "stock": stock, **metrics}
        product_rows.append(row)
        if metrics["units"] == 0:
            zero_sales.append(row)
        previous_units = previous_product_units[product.id]
        if previous_units and metrics["units"] < previous_units:
            row["decline_percent"] = Decimal(previous_units - metrics["units"]) / Decimal(previous_units) * 100
            declining.append(row)
        if stock > max(product.reorder_level * 3, 10) and metrics["units"] <= 1:
            high_stock_weak.append(row)
        if metrics["units"] >= max(2, days // 3) and stock <= product.reorder_level:
            low_stock_demand.append(row)

    product_rows.sort(key=lambda row: (-row["units"], row["name"]))
    # Include every active store, even with zero sales, and expose its current
    # availability so store comparisons do not hide quiet locations.
    store_stock = dict(
        db.session.query(
            StoreInventory.store_id, func.coalesce(func.sum(StoreInventory.quantity), 0)
        ).group_by(StoreInventory.store_id).all()
    )
    for store in Store.query.filter_by(is_active=True).all():
        by_store[(store.id, store.name)]
    store_rows = [
        {"id": key[0], "name": key[1], "stock": int(store_stock.get(key[0], 0)) if key[0] else None, **value}
        for key, value in sorted(by_store.items(), key=lambda pair: (-pair[1]["revenue"], pair[0][1]))
    ]

    recommendations = []
    for row in high_stock_weak[:5]:
        recommendations.append({
            "type": "Slow-moving inventory",
            "message": f"{row['name']} has {row['stock']} units in stock but sold {row['units']} during the selected period. Review its positioning or consider a cost-safe promotion.",
        })
    for row in declining[:5]:
        recommendations.append({
            "type": "Sales decline",
            "message": f"Sales of {row['name']} declined by {row['decline_percent']:.0f}% versus the previous equivalent period.",
        })
    for row in low_stock_demand[:5]:
        recommendations.append({
            "type": "Reorder",
            "message": f"{row['name']} sold {row['units']} units and only {row['stock']} remain. Consider replenishment.",
        })
    if len(store_rows) >= 2 and store_rows[1]["revenue"]:
        advantage = (store_rows[0]["revenue"] / store_rows[1]["revenue"] - 1) * 100
        recommendations.append({
            "type": "Store performance",
            "message": f"{store_rows[0]['name']} generated {advantage:.0f}% more revenue than {store_rows[1]['name']} in this period.",
        })
    for product in products:
        if product.purchase_price and product.retail_price > product.purchase_price:
            # The suggested floor preserves a documented 15% markup on known cost.
            floor = (product.purchase_price * Decimal("1.15")).quantize(Decimal("0.01"))
            if product.retail_price > floor * Decimal("1.10"):
                recommendations.append({
                    "type": "Cost-safe promotion",
                    "message": f"{product.name} could be tested at no less than EGP {floor:,.2f}; this floor preserves a 15% markup on its recorded purchase price.",
                })
                break

    return {
        "start": period_start_date,
        "end": period_end_date_exclusive,
        "display_end": period_end_date_exclusive - timedelta(days=1),
        "previous_start": previous_start_date,
        "query_start": start,
        "query_end": end,
        "summary": summary,
        "previous": previous,
        "revenue_change_percent": (
            (summary["revenue"] - previous["revenue"]) / previous["revenue"] * 100
            if previous["revenue"] else None
        ),
        "products": product_rows,
        "top_products": product_rows[:10],
        "least_products": sorted(
            (row for row in product_rows if row["units"] > 0), key=lambda row: (row["units"], row["name"])
        )[:10],
        "zero_sales": zero_sales,
        "declining": declining,
        "high_stock_weak": high_stock_weak,
        "low_stock_demand": low_stock_demand,
        "stores": store_rows,
        "tiers": [{"tier": tier, **value} for tier, value in by_tier.items()],
        "daily": [{"date": day, "revenue": daily[day]} for day in sorted(daily)],
        "recommendations": recommendations or [{"type": "Monitoring", "message": "No urgent rule-based action was identified for this period."}],
        "employee_metrics_available": any(order.employee_reference for _, order, *_ in rows),
    }
