from importlib import import_module

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Can be run as a cronjob or directly to clean out expired sessions "
        "(only with the database backend at the moment)."
    )

    def handle(self, **options):
        engine = import_module(settings.SESSION_ENGINE)
        try:
            engine.SessionStore.clear_expired()
        except NotImplementedError:
            raise CommandError(
                "Session engine '%s' doesn't support clearing expired "
                "sessions.", logger_args=(settings.SESSION_ENGINE,)
            )
