from unittest import mock

from django.apps.registry import Apps, apps
from django.contrib.contenttypes import management as contenttypes_management
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase, modify_settings

from .models import ModelWithNullFKToSite, Post


@modify_settings(INSTALLED_APPS={'append': ['empty_models', 'no_models']})
class RemoveStaleContentTypesTests(TestCase):
    # Speed up tests by avoiding retrieving ContentTypes for all test apps.
    available_apps = [
        'contenttypes_tests',
        'empty_models',
        'no_models',
        'django.contrib.contenttypes',
    ]

    @classmethod
    def setUpTestData(cls):
        cls.before_count = ContentType.objects.count()
        cls.content_type = ContentType.objects.create(app_label='contenttypes_tests', model='Fake')

    single_content_type_message = (
        'Some content types in your database are stale and can be deleted.\n'
        'Any objects that depend on these content types will also be deleted.\n'
        'The content types and dependent objects that would be deleted are:\n\n'
        '    - Content type for %s.%s\n\n'
        "This list doesn't include any cascade deletions to data outside of Django's\n"
        'models (uncommon).\n\n'
        'Are you sure you want to delete these content types?\n'
        "If you're unsure, answer 'no'."
    )

    def setUp(self):
        self.app_config = apps.get_app_config('contenttypes_tests')

    def test_interactive_true_with_dependent_objects(self):
        """
        interactive mode (the default) deletes stale content types and warns of
        dependent objects.
        """
        post = Post.objects.create(title='post', content_type=self.content_type)
        # A related object is needed to show that a custom collector with
        # can_fast_delete=False is needed.
        ModelWithNullFKToSite.objects.create(post=post)
        with mock.patch('builtins.input', return_value='yes'), self.assertLogs('django.command') as logs:
            call_command('remove_stale_contenttypes', verbosity=2)
        self.assertEqual(Post.objects.count(), 0)
        self.assertEqual(ContentType.objects.count(), self.before_count)
        self.assertLogRecords(logs, [
            ('INFO',
             'Some content types in your database are stale and can be deleted.\n'
             'Any objects that depend on these content types will also be deleted.\n'
             'The content types and dependent objects that would be deleted are:\n\n'
             '    - Content type for %s.%s\n'
             '    - %s %s object(s)\n'
             '    - %s %s object(s)\n\n'
             "This list doesn't include any cascade deletions to data outside of Django's\n"
             'models (uncommon).\n\n'
             'Are you sure you want to delete these content types?\n'
             "If you're unsure, answer 'no'.",
             ('contenttypes_tests', 'Fake', 1, 'contenttypes_tests.Post', 1,
              'contenttypes_tests.ModelWithNullFKToSite')),
            ('INFO', "Deleting stale content type '%s | %s'", ('contenttypes_tests', 'Fake')),
        ])

    def test_interactive_true_without_dependent_objects(self):
        """
        interactive mode deletes stale content types even if there aren't any
        dependent objects.
        """
        with mock.patch('builtins.input', return_value='yes'), self.assertLogs('django.command') as logs:
            call_command('remove_stale_contenttypes', verbosity=2)
        self.assertLogRecords(logs, [
            ('INFO', self.single_content_type_message, ('contenttypes_tests', 'Fake')),
            ('INFO', "Deleting stale content type '%s | %s'", ('contenttypes_tests', 'Fake')),
        ])
        self.assertEqual(ContentType.objects.count(), self.before_count)

    def test_interactive_false(self):
        """non-interactive mode deletes stale content types."""
        with self.assertLogs('django.command') as logs:
            call_command('remove_stale_contenttypes', interactive=False, verbosity=2)
        self.assertLogRecords(logs, [
            ('INFO', "Deleting stale content type '%s | %s'", ('contenttypes_tests', 'Fake')),
        ])
        self.assertEqual(ContentType.objects.count(), self.before_count)

    def test_unavailable_content_type_model(self):
        """A ContentType isn't created if the model isn't available."""
        apps = Apps()
        with self.assertNumQueries(0):
            contenttypes_management.create_contenttypes(self.app_config, interactive=False, verbosity=0, apps=apps)
        self.assertEqual(ContentType.objects.count(), self.before_count + 1)

    @modify_settings(INSTALLED_APPS={'remove': ['empty_models']})
    def test_contenttypes_removed_in_installed_apps_without_models(self):
        ContentType.objects.create(app_label='empty_models', model='Fake 1')
        ContentType.objects.create(app_label='no_models', model='Fake 2')
        with mock.patch('builtins.input', return_value='yes'), self.assertLogs('django.command') as logs:
            call_command('remove_stale_contenttypes', verbosity=2)
        self.assertEqual(ContentType.objects.count(), self.before_count + 1)
        # Fake 1 is not deleted.
        self.assertLogRecords(logs, [
            ('INFO', self.single_content_type_message, ('contenttypes_tests', 'Fake')),
            ('INFO', "Deleting stale content type '%s | %s'", ('contenttypes_tests', 'Fake')),
            ('INFO', self.single_content_type_message, ('no_models', 'Fake 2')),
            ('INFO', "Deleting stale content type '%s | %s'", ('no_models', 'Fake 2')),
        ])

    @modify_settings(INSTALLED_APPS={'remove': ['empty_models']})
    def test_contenttypes_removed_for_apps_not_in_installed_apps(self):
        ContentType.objects.create(app_label='empty_models', model='Fake 1')
        ContentType.objects.create(app_label='no_models', model='Fake 2')
        with mock.patch('builtins.input', return_value='yes'), self.assertLogs('django.command') as logs:
            call_command('remove_stale_contenttypes', include_stale_apps=True, verbosity=2)
        self.assertEqual(ContentType.objects.count(), self.before_count)
        self.assertLogRecords(logs, [
            ('INFO', self.single_content_type_message, ('contenttypes_tests', 'Fake')),
            ('INFO', "Deleting stale content type '%s | %s'", ('contenttypes_tests', 'Fake')),
            ('INFO', self.single_content_type_message, ('empty_models', 'Fake 1')),
            ('INFO', "Deleting stale content type '%s | %s'", ('empty_models', 'Fake 1')),
            ('INFO', self.single_content_type_message, ('no_models', 'Fake 2')),
            ('INFO', "Deleting stale content type '%s | %s'", ('no_models', 'Fake 2')),
        ])
