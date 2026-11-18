import logging
import os
import time

from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AppConfiguration, WorkerHeartbeat, WorkerLog
from mails.fetcher import MailFetcher
from printing.pipeline import process_next_job


logger = logging.getLogger("mailprint.worker")


class Command(BaseCommand):
    help = "Poll Gmail for unseen messages and store them locally."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one poll cycle and exit.",
        )

    def handle(self, *args, **options):
        configuration, _ = AppConfiguration.objects.get_or_create(key=1)
        if not configuration.imap_email or not configuration.imap_app_password_encrypted:
            raise CommandError("Run 'python manage.py configure_mail' before starting the worker.")

        heartbeat, _ = WorkerHeartbeat.objects.get_or_create(key=1)
        heartbeat.running = True
        heartbeat.process_id = os.getpid()
        heartbeat.started_at = timezone.now()
        heartbeat.last_error = ""
        heartbeat.save()
        logger.info("Starting MailPrint worker (pid=%s).", os.getpid())

        try:
            while True:
                configuration.refresh_from_db()
                poll_error = None
                try:
                    fetcher = MailFetcher(
                        configuration,
                        max_email_bytes=config(
                            "MAILPRINT_MAX_EMAIL_BYTES",
                            default=25 * 1024 * 1024,
                            cast=int,
                        ),
                        max_attachment_bytes=config(
                            "MAILPRINT_MAX_ATTACHMENT_BYTES",
                            default=25 * 1024 * 1024,
                            cast=int,
                        ),
                    )
                    summary = fetcher.fetch_unseen()
                except Exception as exc:
                    message = f"Gmail poll failed: {exc}"
                    poll_error = message
                    WorkerLog.objects.create(
                        level=WorkerLog.Level.ERROR,
                        component="imap",
                        message=message,
                    )
                    logger.exception(message)
                    heartbeat.last_error = message
                    heartbeat.last_seen_at = timezone.now()
                    heartbeat.save(update_fields=["last_error", "last_seen_at"])
                    self.stderr.write(self.style.ERROR(message))
                else:
                    heartbeat.last_fetch_at = timezone.now()
                    heartbeat.last_seen_at = heartbeat.last_fetch_at
                    heartbeat.last_error = ""
                    heartbeat.save(
                        update_fields=["last_fetch_at", "last_seen_at", "last_error"]
                    )
                    self.stdout.write(
                        "Gmail poll complete: "
                        f"{summary.stored} stored, {summary.duplicates} duplicates, "
                        f"{summary.errors} errors."
                    )
                    logger.info(
                        "Gmail poll complete: %s stored, %s duplicates, %s errors.",
                        summary.stored,
                        summary.duplicates,
                        summary.errors,
                    )

                processed_jobs = 0
                while True:
                    job = process_next_job()
                    if job is None:
                        break
                    processed_jobs += 1
                if processed_jobs:
                    self.stdout.write(f"Processed {processed_jobs} print job(s).")
                    logger.info("Processed %s print job(s).", processed_jobs)

                if options["once"]:
                    if poll_error:
                        raise CommandError(poll_error)
                    return
                time.sleep(configuration.poll_interval_seconds)
        except KeyboardInterrupt:
            self.stdout.write("Stopping Gmail worker.")
            logger.info("Stopping MailPrint worker.")
        finally:
            heartbeat.refresh_from_db()
            heartbeat.running = False
            heartbeat.process_id = None
            heartbeat.last_seen_at = timezone.now()
            heartbeat.save(
                update_fields=["running", "process_id", "last_seen_at"]
            )
            logger.info("MailPrint worker stopped.")
