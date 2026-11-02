"""Generate deterministic sample PDF data for local layout verification."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reporting import generate_report_pdf


def sample_report():
    products = [
        {"name": "Wireless Mouse", "units": 18, "revenue": Decimal("5400"), "stock": 11},
        {"name": "Mechanical Keyboard", "units": 12, "revenue": Decimal("14400"), "stock": 7},
        {"name": "USB-C Cable", "units": 8, "revenue": Decimal("1200"), "stock": 35},
    ]
    return {
        "start": datetime(2026, 10, 20), "end": datetime(2026, 10, 25),
        "display_end": datetime(2026, 10, 24),
        "summary": {
            "revenue": Decimal("21000"), "units": 38, "orders": 14,
            "average_order_value": Decimal("1500"), "known_profit": Decimal("4900"),
            "profit_coverage_revenue": Decimal("21000"), "margin_percent": Decimal("23.33"),
        },
        "stores": [
            {"name": "Downtown", "units": 22, "stock": 41, "revenue": Decimal("13200")},
            {"name": "Mall Branch", "units": 16, "stock": 29, "revenue": Decimal("7800")},
        ],
        "top_products": products,
        "recommendations": [
            {"type": "Reorder", "message": "Mechanical Keyboard has strong demand and limited stock. Consider replenishment."},
            {"type": "Store performance", "message": "Downtown generated more revenue than Mall Branch during this period."},
            {"type": "Cost-safe promotion", "message": "A promotional floor based on known cost preserves the documented 15% markup."},
        ],
    }


if __name__ == "__main__":
    output = Path("output/pdf/makhzoon-sample-business-report.pdf")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(generate_report_pdf(sample_report(), "Makhzoon Sample Business Performance Report"))
    print(output.resolve())
