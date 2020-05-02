from django.core.management.base import BaseCommand


class Command(BaseCommand):

    def handle(self, **options):
        self.logger.info('complex_app')
