"""PDF generation and explicitly enabled SMTP delivery for business reports."""

import io
import smtplib
from email.message import EmailMessage

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def generate_report_pdf(report, title="Makhzoon Business Performance Report"):
    """Generate a compact, professional PDF entirely in memory."""
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    styles["Title"].textColor = colors.HexColor("#243447")
    styles["Heading2"].textColor = colors.HexColor("#355b73")
    story = [
        Paragraph(title, styles["Title"]),
        Paragraph(
            f"Reporting period: {report['start']:%d %b %Y} to {report['display_end']:%d %b %Y}",
            styles["Normal"],
        ),
        Spacer(1, 8),
    ]
    summary = report["summary"]
    summary_data = [
        ["Revenue", "Units", "Orders", "Average order"],
        [f"EGP {summary['revenue']:,.2f}", str(summary["units"]), str(summary["orders"]), f"EGP {summary['average_order_value']:,.2f}"],
    ]
    table = Table(summary_data, colWidths=[42 * mm] * 4)
    table.setStyle(_table_style())
    story += [table, Spacer(1, 12), Paragraph("Store Performance", styles["Heading2"])]
    store_data = [["Store", "Units", "Available", "Revenue"]] + [
        [row["name"], str(row["units"]), str(row.get("stock", "Unknown")), f"EGP {row['revenue']:,.2f}"] for row in report["stores"]
    ]
    store_table = Table(store_data, colWidths=[68 * mm, 26 * mm, 31 * mm, 42 * mm], repeatRows=1)
    store_table.setStyle(_table_style())
    story += [store_table, Spacer(1, 12)]
    story.append(Paragraph("Product Performance", styles["Heading2"]))
    product_data = [["Product", "Units", "Revenue", "Stock"]] + [
        [row["name"], str(row["units"]), f"EGP {row['revenue']:,.2f}", str(row["stock"])]
        for row in report["top_products"]
    ]
    product_table = Table(product_data, colWidths=[75 * mm, 25 * mm, 45 * mm, 25 * mm], repeatRows=1)
    product_table.setStyle(_table_style())
    story += [product_table, PageBreak(), Paragraph("Key Findings and Recommendations", styles["Heading2"])]
    for recommendation in report["recommendations"]:
        story += [Paragraph(f"<b>{recommendation['type']}:</b> {recommendation['message']}", styles["BodyText"]), Spacer(1, 6)]
    if summary["margin_percent"] is None:
        story.append(Paragraph("Gross profit is not confirmed where historical purchase-cost snapshots are unavailable.", styles["Italic"]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#68747f"))
        canvas.drawString(16 * mm, 9 * mm, "Makhzoon")
        canvas.drawRightString(194 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _table_style():
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#355b73")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dfe4e8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ])


def send_report_email(config, recipient, subject, pdf_bytes, filename):
    """Send only when explicitly enabled and every required SMTP value exists."""
    if not config.get("ENABLE_REPORT_EMAIL"):
        raise RuntimeError("Report email is disabled. Set ENABLE_REPORT_EMAIL=true explicitly.")
    required = ["SMTP_HOST", "SMTP_FROM"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RuntimeError(f"Missing email configuration: {', '.join(missing)}")
    message = EmailMessage()
    message["From"] = config["SMTP_FROM"]
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("Your scheduled Makhzoon business report is attached.")
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=30) as smtp:
        if config.get("SMTP_USE_TLS"):
            smtp.starttls()
        if config.get("SMTP_USERNAME"):
            smtp.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
        smtp.send_message(message)
