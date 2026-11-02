# config.py
# Stores application configuration settings.
# We keep this separate so database credentials are not hard-coded in the main application.

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# Get the absolute path of the current directory
basedir = os.path.abspath(os.path.dirname(__file__))

# Load the project-local file explicitly so both Flask CLI and direct VS Code
# launches (for example, "Python: Current File") resolve the same credentials.
load_dotenv(Path(basedir) / ".env", override=False)


class Config:
    """Environment-driven settings; no deployable secret is committed."""

    SECRET_KEY = os.getenv("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    BUSINESS_TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "Africa/Cairo")
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    CSRF_ENABLED = True
    LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
    LOGIN_ATTEMPT_WINDOW_MINUTES = int(os.getenv("LOGIN_ATTEMPT_WINDOW_MINUTES", "15"))
    PASSWORD_RESET_MINUTES = int(os.getenv("PASSWORD_RESET_MINUTES", "60"))
    PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    REPORT_OUTPUT_DIR = os.getenv(
        "REPORT_OUTPUT_DIR", str(Path(basedir) / "output" / "pdf")
    )
    ENABLE_REPORT_EMAIL = os.getenv("ENABLE_REPORT_EMAIL", "false").lower() == "true"
    REPORT_EMAIL_TO = os.getenv("REPORT_EMAIL_TO", "")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    ENABLE_AUTH_EMAIL = os.getenv("ENABLE_AUTH_EMAIL", "false").lower() == "true"
