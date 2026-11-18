from getpass import getpass

from django.core.management.base import BaseCommand, CommandError

from core.models import AppConfiguration


class Command(BaseCommand):
    help = "Securely store the Gmail account and App Password used by MailPrint."

    def handle(self, *args, **options):
        email = input("Gmail address: ").strip()
        password = getpass("Gmail App Password: ").strip().replace(" ", "")
        confirmation = getpass("Confirm Gmail App Password: ").strip().replace(" ", "")
        if not email or not password:
            raise CommandError("Gmail address and App Password are required.")
        if password != confirmation:
            raise CommandError("The App Password entries did not match.")

        configuration, _ = AppConfiguration.objects.get_or_create(key=1)
        configuration.imap_email = email
        configuration.set_imap_app_password(password)
        configuration.set_smtp_app_password(password)
        configuration.save(
            update_fields=[
                "imap_email",
                "imap_app_password_encrypted",
                "smtp_app_password_encrypted",
                "updated_at",
            ]
        )
        self.stdout.write(self.style.SUCCESS("Gmail credentials encrypted and saved."))
