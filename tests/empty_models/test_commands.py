from django.core.management import call_command
from django.test import TestCase


class CoreCommandsNoOutputTests(TestCase):
    available_apps = ['empty_models']

    def test_sqlflush_no_tables(self):
        with self.assertLogs('django.command', 'ERROR') as logs:
            call_command('sqlflush')
        [log] = logs.records
        self.assertEqual(log.levelname, 'ERROR')
        self.assertEqual(log.msg, 'No tables found.')

    def test_sqlsequencereset_no_sequences(self):
        with self.assertLogs('django.command', 'ERROR') as logs:
            call_command('sqlsequencereset', 'empty_models')
        [log] = logs.records
        self.assertEqual(log.levelname, 'ERROR')
        self.assertEqual(log.msg, 'No sequences found.')
