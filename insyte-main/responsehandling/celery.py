import os

from celery import Celery
from dotenv import load_dotenv

# Load .env before the setdefault so that DJANGO_SETTINGS_MODULE defined in
# .env (e.g. responsehandling.settings.development) takes effect when the
# worker is started without the variable already exported in the shell.
load_dotenv(".env")

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "responsehandling.settings.development",
)

app = Celery("responsehandling")

# Load Django settings with CELERY_ namespace
app.config_from_object("django.conf:settings", namespace="CELERY")


broker_url = os.getenv("CELERY_BROKER_URL")
result_backend = os.getenv("CELERY_RESULT_BACKEND")

if broker_url:
    app.conf.broker_url = broker_url

if result_backend:
    app.conf.result_backend = result_backend

# ======================================================
# Safety defaults (prevents silent failures)
# ======================================================

# Safety defaults already in Django settings (CELERY_ namespace).
# config_from_object above loads them automatically.

# ======================================================
# Auto-discover Django tasks
# ======================================================

# autodiscover_tasks() only discovers modules named "tasks.py" by default.
# scan_tasks.py and letter_tasks.py contain additional Celery tasks and must
# be explicitly included so the worker registers them.
app.autodiscover_tasks()
app.autodiscover_tasks(related_name="scan_tasks")
app.autodiscover_tasks(related_name="letter_tasks")
