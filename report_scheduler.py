"""Daily scheduler entry point for idempotent five-day and monthly emails.

Run this command once per day from cron or Windows Task Scheduler. The database
unique constraint prevents duplicate delivery for the same schedule and period.
"""

import argparse
from datetime import timedelta

from analytics import build_report
from app import app
from models import db, ReportDelivery, utc_now
from reporting import generate_report_pdf, send_report_email
from time_utils import local_date_range_to_utc, local_today


def deliver(schedule_type, start_date, end_date):
    recipient = app.config.get("REPORT_EMAIL_TO", "").strip()
    if not recipient:
        raise RuntimeError("REPORT_EMAIL_TO is required for scheduled delivery.")
    delivery = ReportDelivery.query.filter_by(
        schedule_type=schedule_type, period_start=start_date, period_end=end_date
    ).first()
    if delivery and delivery.status == "sent":
        return "already sent"
    if delivery is None:
        delivery = ReportDelivery(
            schedule_type=schedule_type, period_start=start_date,
            period_end=end_date, recipient=recipient, attempts=0,
        )
        db.session.add(delivery)
    delivery.attempts += 1
    delivery.status = "pending"
    db.session.commit()
    try:
        end_date_exclusive = end_date + timedelta(days=1)
        timezone_name = app.config["BUSINESS_TIMEZONE"]
        start, end = local_date_range_to_utc(
            start_date, end_date_exclusive, timezone_name
        )
        pdf = generate_report_pdf(build_report(
            start, end, timezone_name, start_date, end_date_exclusive
        ))
        filename = f"makhzoon-{schedule_type}-{start_date}-{end_date}.pdf"
        send_report_email(
            app.config, recipient, f"Makhzoon {schedule_type} report: {start_date} to {end_date}",
            pdf, filename,
        )
    except Exception as exc:
        delivery.status = "failed"
        delivery.error_message = str(exc)[:2000]
        db.session.commit()
        raise
    delivery.status = "sent"
    delivery.sent_at = utc_now()
    delivery.error_message = None
    db.session.commit()
    return "sent"


def run_due(force=False):
    today = local_today(app.config["BUSINESS_TIMEZONE"])
    results = []
    if force or today.toordinal() % 5 == 0:
        results.append(("five_day", deliver("five_day", today - timedelta(days=5), today - timedelta(days=1))))
    if force or today.day == 1:
        previous_end = today.replace(day=1) - timedelta(days=1)
        results.append(("monthly", deliver("monthly", previous_end.replace(day=1), previous_end)))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run both schedules now; idempotency still applies.")
    args = parser.parse_args()
    with app.app_context():
        for schedule, result in run_due(args.force):
            print(f"{schedule}: {result}")
