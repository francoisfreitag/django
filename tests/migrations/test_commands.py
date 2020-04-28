import datetime
import importlib
import io
import os
import sys
from unittest import mock

from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import (
    ConnectionHandler, DatabaseError, OperationalError, connection,
    connections, models,
)
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.backends.utils import truncate_name
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, override_settings, skipUnlessDBFeature

from .models import UnicodeModel, UnserializableModel
from .routers import TestRouter
from .test_base import MigrationTestBase


def combine_logs(logs):
    return "\n".join(logs.output)


class MigrateTests(MigrationTestBase):
    """
    Tests running the migrate command.
    """
    databases = {'default', 'other'}

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations"})
    def test_migrate(self):
        """
        Tests basic usage of the migrate command.
        """
        # No tables are created
        self.assertTableNotExists("migrations_author")
        self.assertTableNotExists("migrations_tribble")
        self.assertTableNotExists("migrations_book")
        # Run the migrations to 0001 only
        with self.assertLogs('django.command') as command_logs, self.assertLogs('django.progress') as progress_logs:
            call_command('migrate', 'migrations', '0001', verbosity=1, no_color=True)

        self.assertLogRecords(command_logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Target specific migration: %s, from %s', ('0001_initial', 'migrations')),
            ('INFO', 'Running migrations:', ()),
        ])
        self.assertLogRecords(progress_logs, [
            ('INFO', '  Applying %s...', ('migrations.0001_initial',)),
            ('INFO', ' OK%s', ('',)),
            ('INFO', '\n', ()),
        ])
        # The correct tables exist
        self.assertTableExists("migrations_author")
        self.assertTableExists("migrations_tribble")
        self.assertTableNotExists("migrations_book")
        # Run migrations all the way
        call_command("migrate", verbosity=0)
        # The correct tables exist
        self.assertTableExists("migrations_author")
        self.assertTableNotExists("migrations_tribble")
        self.assertTableExists("migrations_book")
        # Unmigrate everything
        with self.assertLogs('django.command') as command_logs, self.assertLogs('django.progress') as progress_logs:
            call_command('migrate', 'migrations', 'zero', verbosity=1, no_color=True)
        self.assertLogRecords(command_logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Unapply all migrations: %s', ('migrations',)),
            ('INFO', 'Running migrations:', ()),
        ])
        self.assertLogRecords(progress_logs, [
            ('INFO', '  Rendering model states...', ()),
            ('INFO', ' DONE%s\n', ('',)),
            ('INFO', '  Unapplying %s...', ('migrations.0002_second',)),
            ('INFO', ' OK%s', ('',)),
            ('INFO', '\n', ()),
            ('INFO', '  Unapplying %s...', ('migrations.0001_initial',)),
            ('INFO', ' OK%s', ('',)),
            ('INFO', '\n', ()),
        ])
        # Tables are gone
        self.assertTableNotExists("migrations_author")
        self.assertTableNotExists("migrations_tribble")
        self.assertTableNotExists("migrations_book")

    @override_settings(INSTALLED_APPS=[
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'migrations.migrations_test_apps.migrated_app',
    ])
    def test_migrate_with_system_checks(self):
        with self.assertLogs('django.command') as command_logs, self.assertLogs('django.progress') as progress_logs:
            call_command('migrate', skip_checks=False, no_color=True)
        self.assertLogRecords(command_logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Apply all migrations: %s', ('migrated_app',)),
            ('INFO', 'Running migrations:', ()),
        ])
        self.assertLogRecords(progress_logs, [
            ('INFO', '  Applying %s...', ('migrated_app.0001_initial',)),
            ('INFO', ' OK%s', ('',)),
            ('INFO', '\n', ()),
        ])

    @override_settings(INSTALLED_APPS=['migrations', 'migrations.migrations_test_apps.unmigrated_app_syncdb'])
    def test_app_without_migrations(self):
        with self.assertRaises(CommandError) as cm:
            call_command('migrate', app_label='unmigrated_app_syncdb')
        [message] = cm.exception.args
        self.assertEqual(message, "App '%s' does not have migrations.")
        self.assertEqual(cm.exception.logger_args, ('unmigrated_app_syncdb',))

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_clashing_prefix'})
    def test_ambiguous_prefix(self):
        with self.assertRaises(CommandError) as cm:
            call_command('migrate', app_label='migrations', migration_name='a')
        [message] = cm.exception.args
        self.assertEqual(message, "More than one migration matches '%s' in app '%s'. Please be more specific.")
        self.assertEqual(cm.exception.logger_args, ('a', 'migrations'))

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations'})
    def test_unknown_prefix(self):
        with self.assertRaises(CommandError) as cm:
            call_command('migrate', app_label='migrations', migration_name='nonexistent')
        [message] = cm.exception.args
        self.assertEqual(message, "Cannot find a migration matching '%s' from app '%s'.")
        self.assertEqual(cm.exception.logger_args, ('nonexistent', 'migrations'))

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_initial_false"})
    def test_migrate_initial_false(self):
        """
        `Migration.initial = False` skips fake-initial detection.
        """
        # Make sure no tables are created
        self.assertTableNotExists("migrations_author")
        self.assertTableNotExists("migrations_tribble")
        # Run the migrations to 0001 only
        call_command("migrate", "migrations", "0001", verbosity=0)
        # Fake rollback
        call_command("migrate", "migrations", "zero", fake=True, verbosity=0)
        # Make sure fake-initial detection does not run
        with self.assertRaises(DatabaseError):
            call_command("migrate", "migrations", "0001", fake_initial=True, verbosity=0)

        call_command("migrate", "migrations", "0001", fake=True, verbosity=0)
        # Real rollback
        call_command("migrate", "migrations", "zero", verbosity=0)
        # Make sure it's all gone
        self.assertTableNotExists("migrations_author")
        self.assertTableNotExists("migrations_tribble")
        self.assertTableNotExists("migrations_book")

    @override_settings(
        MIGRATION_MODULES={"migrations": "migrations.test_migrations"},
        DATABASE_ROUTERS=['migrations.routers.TestRouter'],
    )
    def test_migrate_fake_initial(self):
        """
        --fake-initial only works if all tables created in the initial
        migration of an app exists. Database routers must be obeyed when doing
        that check.
        """
        # Make sure no tables are created
        for db in self.databases:
            self.assertTableNotExists("migrations_author", using=db)
            self.assertTableNotExists("migrations_tribble", using=db)
        # Run the migrations to 0001 only
        call_command("migrate", "migrations", "0001", verbosity=0)
        call_command("migrate", "migrations", "0001", verbosity=0, database="other")
        # Make sure the right tables exist
        self.assertTableExists("migrations_author")
        self.assertTableNotExists("migrations_tribble")
        # Also check the "other" database
        self.assertTableNotExists("migrations_author", using="other")
        self.assertTableExists("migrations_tribble", using="other")

        # Fake a roll-back
        call_command("migrate", "migrations", "zero", fake=True, verbosity=0)
        call_command("migrate", "migrations", "zero", fake=True, verbosity=0, database="other")
        # Make sure the tables still exist
        self.assertTableExists("migrations_author")
        self.assertTableExists("migrations_tribble", using="other")
        # Try to run initial migration
        with self.assertRaises(DatabaseError):
            call_command("migrate", "migrations", "0001", verbosity=0)
        # Run initial migration with an explicit --fake-initial
        with mock.patch('django.core.management.color.supports_color', lambda *args: False):
            with self.assertLogs(
                'django.command',
            ) as command_logs, self.assertLogs('django.progress') as progress_logs:
                call_command("migrate", "migrations", "0001", fake_initial=True, verbosity=1)
            call_command("migrate", "migrations", "0001", fake_initial=True, verbosity=0, database="other")
        self.assertLogRecords(command_logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Target specific migration: %s, from %s', ('0001_initial', 'migrations')),
            ('INFO', 'Running migrations:', ()),
        ])
        self.assertLogRecords(progress_logs, [
            ('INFO', '  Applying %s...', ('migrations.0001_initial',)),
            ('INFO', ' FAKED%s', ('',)),
            ('INFO', '\n', ()),
        ])
        try:
            # Run migrations all the way.
            call_command('migrate', verbosity=0)
            call_command('migrate', verbosity=0, database='other')
            self.assertTableExists('migrations_author')
            self.assertTableNotExists('migrations_tribble')
            self.assertTableExists('migrations_book')
            self.assertTableNotExists('migrations_author', using='other')
            self.assertTableNotExists('migrations_tribble', using='other')
            self.assertTableNotExists('migrations_book', using='other')
            # Fake a roll-back
            call_command('migrate', 'migrations', 'zero', fake=True, verbosity=0)
            call_command('migrate', 'migrations', 'zero', fake=True, verbosity=0, database='other')
            self.assertTableExists('migrations_author')
            self.assertTableNotExists('migrations_tribble')
            self.assertTableExists('migrations_book')
            # Run initial migration.
            with self.assertRaises(DatabaseError):
                call_command('migrate', 'migrations', verbosity=0)
            # Run initial migration with an explicit --fake-initial.
            with self.assertRaises(DatabaseError):
                # Fails because "migrations_tribble" does not exist but needs
                # to in order to make --fake-initial work.
                call_command('migrate', 'migrations', fake_initial=True, verbosity=0)
            # Fake an apply.
            call_command('migrate', 'migrations', fake=True, verbosity=0)
            call_command('migrate', 'migrations', fake=True, verbosity=0, database='other')
        finally:
            # Unmigrate everything.
            call_command('migrate', 'migrations', 'zero', verbosity=0)
            call_command('migrate', 'migrations', 'zero', verbosity=0, database='other')
        # Make sure it's all gone
        for db in self.databases:
            self.assertTableNotExists("migrations_author", using=db)
            self.assertTableNotExists("migrations_tribble", using=db)
            self.assertTableNotExists("migrations_book", using=db)

    @skipUnlessDBFeature('ignores_table_name_case')
    def test_migrate_fake_initial_case_insensitive(self):
        with override_settings(MIGRATION_MODULES={
            'migrations': 'migrations.test_fake_initial_case_insensitive.initial',
        }):
            call_command('migrate', 'migrations', '0001', verbosity=0)
            call_command('migrate', 'migrations', 'zero', fake=True, verbosity=0)

        with override_settings(MIGRATION_MODULES={
            'migrations': 'migrations.test_fake_initial_case_insensitive.fake_initial',
        }):
            with self.assertLogs(
                'django.command'
            ) as command_logs, self.assertLogs('django.progress') as progress_logs:
                call_command('migrate', 'migrations', '0001', fake_initial=True, verbosity=1, no_color=True)
        self.assertLogRecords(command_logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Target specific migration: %s, from %s', ('0001_initial', 'migrations')),
            ('INFO', 'Running migrations:', ()),
        ])
        self.assertLogRecords(progress_logs, [
            ('INFO', '  Applying %s...', ('migrations.0001_initial',)),
            ('INFO', ' FAKED%s', ('',)),
            ('INFO', '\n', ()),
        ])

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_fake_split_initial"})
    def test_migrate_fake_split_initial(self):
        """
        Split initial migrations can be faked with --fake-initial.
        """
        try:
            call_command('migrate', 'migrations', '0002', verbosity=0)
            call_command('migrate', 'migrations', 'zero', fake=True, verbosity=0)
            with (
                mock.patch('django.core.management.color.supports_color', lambda *args: False),
                self.assertLogs('django.command') as command_logs,
                self.assertLogs('django.progress') as progress_logs,
            ):
                call_command('migrate', 'migrations', '0002', fake_initial=True, verbosity=1)
            self.assertLogRecords(command_logs, [
                ('INFO', 'Operations to perform:', ()),
                ('INFO', '  Target specific migration: %s, from %s', ('0002_second', 'migrations')),
                ('INFO', 'Running migrations:', ()),
            ])
            self.assertLogRecords(progress_logs, [
                ('INFO', '  Applying %s...', ('migrations.0001_initial',)),
                ('INFO', ' FAKED%s', ('',)),
                ('INFO', '\n', ()),
                ('INFO', '  Applying %s...', ('migrations.0002_second',)),
                ('INFO', ' FAKED%s', ('',)),
                ('INFO', '\n', ()),
            ])
        finally:
            # Fake an apply.
            call_command('migrate', 'migrations', fake=True, verbosity=0)
            # Unmigrate everything.
            call_command('migrate', 'migrations', 'zero', verbosity=0)

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_conflict"})
    def test_migrate_conflict_exit(self):
        """
        migrate exits if it detects a conflict.
        """
        with self.assertRaises(CommandError) as cm:
            call_command("migrate", "migrations")
        [message] = cm.exception.args
        self.assertEqual(
            message,
            "Conflicting migrations detected; multiple leaf nodes in the "
            "migration graph: (%s, %s in %s).\n"
            "To fix them run 'python manage.py makemigrations --merge'"
        )
        self.assertEqual(cm.exception.logger_args, ("0002_conflicting_second", "0002_second", "migrations"))

    @override_settings(MIGRATION_MODULES={
        'migrations': 'migrations.test_migrations',
    })
    def test_migrate_check(self):
        with self.assertRaises(SystemExit):
            call_command('migrate', 'migrations', '0001', check_unapplied=True)
        self.assertTableNotExists('migrations_author')
        self.assertTableNotExists('migrations_tribble')
        self.assertTableNotExists('migrations_book')

    @override_settings(MIGRATION_MODULES={
        'migrations': 'migrations.test_migrations_plan',
    })
    def test_migrate_check_plan(self):
        with self.assertRaises(SystemExit), self.assertLogs('django.command') as logs:
            call_command(
                'migrate',
                'migrations',
                '0001',
                check_unapplied=True,
                plan=True,
                no_color=True,
            )
        self.assertLogRecords(logs, [
            ('INFO', 'Planned operations:', ()),
            ('INFO', 'migrations.0001_initial', ()),
            ('INFO', '    %s', ('Create model Salamander',)),
            ('INFO', '    %s', ('Raw Python operation -> Grow salamander tail.',)),
        ])

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations"})
    def test_showmigrations_list(self):
        """
        showmigrations --list  displays migrations and whether or not they're
        applied.
        """
        with mock.patch(
            'django.core.management.color.supports_color', lambda *args: True,
        ), self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='list', verbosity=0, no_color=False)
        self.assertLogRecords(logs, [
            ('INFO', '\x1b[1m%s\x1b[0m', ('migrations',)),
            ('INFO', ' [ ] %s', ('0001_initial',)),
            ('INFO', ' [ ] %s', ('0002_second',)),
        ])

        call_command("migrate", "migrations", "0001", verbosity=0)

        # Giving the explicit app_label tests for selective `show_list` in the command
        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", "migrations", format='list', verbosity=0, no_color=True)
        self.assertLogRecords(logs, [
            ('INFO', '%s', ('migrations',)),
            ('INFO', ' [X] %s', ('0001_initial',)),
            ('INFO', ' [ ] %s', ('0002_second',)),
        ])
        # Applied datetimes are displayed at verbosity 2+.
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'migrations', verbosity=2, no_color=True)
        migration1 = MigrationRecorder(connection).migration_qs.get(app='migrations', name='0001_initial')
        self.assertLogRecords(logs, [
            ('INFO', '%s', ('migrations',)),
            ('INFO',
             ' [X] %s (applied at %s)',
             ('0001_initial', migration1.applied.strftime('%Y-%m-%d %H:%M:%S'))),
            ('INFO', ' [ ] %s', ('0002_second',)),
        ])
        # Cleanup by unmigrating everything
        call_command("migrate", "migrations", "zero", verbosity=0)

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_run_before"})
    def test_showmigrations_plan(self):
        """
        Tests --plan output of showmigrations command
        """
        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[ ]  %s.%s', ('migrations', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('migrations', '0003_third')),
            ('INFO', '[ ]  %s.%s', ('migrations', '0002_second')),
        ])

        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan', verbosity=2)
        self.assertLogRecords(logs, [
            ('INFO', '[ ]  %s.%s', ('migrations', '0001_initial')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '0003_third', 'migrations', '0001_initial')),
            ('INFO',
             '[ ]  %s.%s ... (%s.%s, %s.%s)',
             ('migrations',
              '0002_second',
              'migrations',
              '0001_initial',
              'migrations',
              '0003_third')),
        ])
        call_command("migrate", "migrations", "0003", verbosity=0)

        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[X]  %s.%s', ('migrations', '0001_initial')),
            ('INFO', '[X]  %s.%s', ('migrations', '0003_third')),
            ('INFO', '[ ]  %s.%s', ('migrations', '0002_second')),
        ])

        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan', verbosity=2)
        self.assertLogRecords(logs, [
            ('INFO', '[X]  %s.%s', ('migrations', '0001_initial')),
            ('INFO', '[X]  %s.%s ... (%s.%s)', ('migrations', '0003_third', 'migrations', '0001_initial')),
            ('INFO',
             '[ ]  %s.%s ... (%s.%s, %s.%s)',
             ('migrations',
              '0002_second',
              'migrations',
              '0001_initial',
              'migrations',
              '0003_third')),
        ])

        # Cleanup by unmigrating everything
        call_command("migrate", "migrations", "zero", verbosity=0)

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_plan'})
    def test_migrate_plan(self):
        """Tests migrate --plan output."""
        # Show the plan up to the third migration.
        with self.assertLogs('django.command') as logs:
            call_command('migrate', 'migrations', '0003', plan=True, no_color=True)
        self.assertLogRecords(logs, [
            ('INFO', 'Planned operations:', ()),
            ('INFO', 'migrations.0001_initial', ()),
            ('INFO', '    %s', ('Create model Salamander',)),
            ('INFO', '    %s', ('Raw Python operation -> Grow salamander tail.',)),
            ('INFO', 'migrations.0002_second', ()),
            ('INFO', '    %s', ('Create model Book',)),
            ('INFO', '    %s', ("Raw SQL operation -> ['SELECT * FROM migrations_book']",)),
            ('INFO', 'migrations.0003_third', ()),
            ('INFO', '    %s', ('Create model Author',)),
            ('INFO', '    %s', ("Raw SQL operation -> ['SELECT * FROM migrations_author']",)),
        ])
        try:
            # Migrate to the third migration.
            call_command('migrate', 'migrations', '0003', verbosity=0)
            # Show the plan for when there is nothing to apply.
            with self.assertLogs('django.command') as logs:
                call_command('migrate', 'migrations', '0003', plan=True, no_color=True)
            self.assertLogRecords(logs, [
                ('INFO', 'Planned operations:', ()),
                ('INFO', '  No planned migration operations.', ()),
            ])
            # Show the plan for reverse migration back to 0001.
            with self.assertLogs('django.command') as logs:
                call_command('migrate', 'migrations', '0001', plan=True, no_color=True)
            self.assertLogRecords(logs, [
                ('INFO', 'Planned operations:', ()),
                ('INFO', 'migrations.0003_third', ()),
                ('INFO', '    %s', ('Undo Create model Author',)),
                ('INFO', '    %s', ("Raw SQL operation -> ['SELECT * FROM migrations_book']",)),
                ('INFO', 'migrations.0002_second', ()),
                ('INFO', '    %s', ('Undo Create model Book',)),
                ('INFO', '    %s', ("Raw SQL operation -> ['SELECT * FROM migrations_salamand…",)),
            ])

            # Show the migration plan to fourth, with truncated details.
            with self.assertLogs('django.command') as logs:
                call_command('migrate', 'migrations', '0004', plan=True, no_color=True)
            self.assertLogRecords(logs, [
                ('INFO', 'Planned operations:', ()),
                ('INFO', 'migrations.0004_fourth', ()),
                ('INFO', '    %s', ('Raw SQL operation -> SELECT * FROM migrations_author WHE…',)),
            ])
            # Show the plan when an operation is irreversible.
            # Migrate to the fourth migration.
            call_command('migrate', 'migrations', '0004', verbosity=0)
            with self.assertLogs('django.command') as logs:
                call_command('migrate', 'migrations', '0003', plan=True, no_color=True)
            self.assertLogRecords(logs, [
                ('INFO', 'Planned operations:', ()),
                ('INFO', 'migrations.0004_fourth', ()),
                ('INFO', '    %s', ('Raw SQL operation -> IRREVERSIBLE',)),
            ])
            with self.assertLogs('django.command') as logs:
                call_command('migrate', 'migrations', '0005', plan=True, no_color=True)
            # Operation is marked as irreversible only in the revert plan.
            self.assertLogRecords(logs, [
                ('INFO', 'Planned operations:', ()),
                ('INFO', 'migrations.0005_fifth', ()),
                ('INFO', '    %s', ('Raw Python operation',)),
                ('INFO', '    %s', ('Raw Python operation',)),
                ('INFO', '    %s', ('Raw Python operation -> Feed salamander.',)),
            ])
            call_command('migrate', 'migrations', '0005', verbosity=0)
            with self.assertLogs('django.command') as logs:
                call_command('migrate', 'migrations', '0004', plan=True, no_color=True)
            self.assertLogRecords(logs, [
                ('INFO', 'Planned operations:', ()),
                ('INFO', 'migrations.0005_fifth', ()),
                ('INFO', '    %s', ('Raw Python operation -> IRREVERSIBLE',)),
                ('INFO', '    %s', ('Raw Python operation -> IRREVERSIBLE',)),
                ('INFO', '    %s', ('Raw Python operation',)),
            ])
        finally:
            # Cleanup by unmigrating everything: fake the irreversible, then
            # migrate all to zero.
            call_command('migrate', 'migrations', '0003', fake=True, verbosity=0)
            call_command('migrate', 'migrations', 'zero', verbosity=0)

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_empty'})
    def test_showmigrations_no_migrations(self):
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', no_color=True)
        self.assertLogRecords(logs, [
            ('INFO', '%s', ('migrations',)),
            ('INFO', ' (no migrations)', ()),
        ])

    @override_settings(INSTALLED_APPS=['migrations.migrations_test_apps.unmigrated_app'])
    def test_showmigrations_unmigrated_app(self):
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'unmigrated_app', no_color=True)
        self.assertLogRecords(logs, [
            ('INFO', '%s', ('unmigrated_app',)),
            ('INFO', ' (no migrations)', ()),
        ])

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_empty"})
    def test_showmigrations_plan_no_migrations(self):
        """
        Tests --plan output of showmigrations command without migrations
        """
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', format='plan', no_color=True)
        self.assertLogRecords(logs, [('INFO', '(no migrations)', ())])

        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', format='plan', verbosity=2, no_color=True)
        self.assertLogRecords(logs, [('INFO', '(no migrations)', ())])

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_squashed_complex"})
    def test_showmigrations_plan_squashed(self):
        """
        Tests --plan output of showmigrations command with squashed migrations.
        """
        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[ ]  %s.%s', ('migrations', '1_auto')),
            ('INFO', '[ ]  %s.%s', ('migrations', '2_auto')),
            ('INFO', '[ ]  %s.%s', ('migrations', '3_squashed_5')),
            ('INFO', '[ ]  %s.%s', ('migrations', '6_auto')),
            ('INFO', '[ ]  %s.%s', ('migrations', '7_auto')),
        ])

        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan', verbosity=2)
        self.assertLogRecords(logs, [
            ('INFO', '[ ]  %s.%s', ('migrations', '1_auto')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '2_auto', 'migrations', '1_auto')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '3_squashed_5', 'migrations', '2_auto')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '6_auto', 'migrations', '3_squashed_5')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '7_auto', 'migrations', '6_auto')),
        ])

        call_command("migrate", "migrations", "3_squashed_5", verbosity=0)

        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[X]  %s.%s', ('migrations', '1_auto')),
            ('INFO', '[X]  %s.%s', ('migrations', '2_auto')),
            ('INFO', '[X]  %s.%s', ('migrations', '3_squashed_5')),
            ('INFO', '[ ]  %s.%s', ('migrations', '6_auto')),
            ('INFO', '[ ]  %s.%s', ('migrations', '7_auto')),
        ])

        with self.assertLogs('django.command') as logs:
            call_command("showmigrations", format='plan', verbosity=2)
        self.assertLogRecords(logs, [
            ('INFO', '[X]  %s.%s', ('migrations', '1_auto')),
            ('INFO', '[X]  %s.%s ... (%s.%s)', ('migrations', '2_auto', 'migrations', '1_auto')),
            ('INFO', '[X]  %s.%s ... (%s.%s)', ('migrations', '3_squashed_5', 'migrations', '2_auto')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '6_auto', 'migrations', '3_squashed_5')),
            ('INFO', '[ ]  %s.%s ... (%s.%s)', ('migrations', '7_auto', 'migrations', '6_auto')),
        ])

    @override_settings(INSTALLED_APPS=[
        'migrations.migrations_test_apps.mutate_state_b',
        'migrations.migrations_test_apps.alter_fk.author_app',
        'migrations.migrations_test_apps.alter_fk.book_app',
    ])
    def test_showmigrations_plan_single_app_label(self):
        """
        `showmigrations --plan app_label` output with a single app_label.
        """
        # Single app with no dependencies on other apps.
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'mutate_state_b', format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[ ]  %s.%s', ('mutate_state_b', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('mutate_state_b', '0002_add_field')),
        ])
        # Single app with dependencies.
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'author_app', format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[ ]  %s.%s', ('author_app', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('book_app', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('author_app', '0002_alter_id')),
        ])
        # Some migrations already applied.
        call_command('migrate', 'author_app', '0001', verbosity=0)
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'author_app', format='plan')
        self.assertLogRecords(logs, [
            ('INFO', '[X]  %s.%s', ('author_app', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('book_app', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('author_app', '0002_alter_id')),
        ])
        # Cleanup by unmigrating author_app.
        call_command('migrate', 'author_app', 'zero', verbosity=0)

    @override_settings(INSTALLED_APPS=[
        'migrations.migrations_test_apps.mutate_state_b',
        'migrations.migrations_test_apps.alter_fk.author_app',
        'migrations.migrations_test_apps.alter_fk.book_app',
    ])
    def test_showmigrations_plan_multiple_app_labels(self):
        """
        `showmigrations --plan app_label` output with multiple app_labels.
        """
        # Multiple apps: author_app depends on book_app; mutate_state_b doesn't
        # depend on other apps.
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'mutate_state_b', 'author_app', format='plan')
        expected = [
            ('INFO', '[ ]  %s.%s', ('author_app', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('book_app', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('author_app', '0002_alter_id')),
            ('INFO', '[ ]  %s.%s', ('mutate_state_b', '0001_initial')),
            ('INFO', '[ ]  %s.%s', ('mutate_state_b', '0002_add_field')),
        ]
        self.assertLogRecords(logs, expected)
        # Multiple apps: args order shouldn't matter (the same result is
        # expected as above).
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'author_app', 'mutate_state_b', format='plan')
        self.assertLogRecords(logs, expected)

    @override_settings(INSTALLED_APPS=['migrations.migrations_test_apps.unmigrated_app'])
    def test_showmigrations_plan_app_label_no_migrations(self):
        with self.assertLogs('django.command') as logs:
            call_command('showmigrations', 'unmigrated_app', format='plan', no_color=True)
        self.assertLogRecords(logs, [('INFO', '(no migrations)', ())])

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations"})
    def test_sqlmigrate_forwards(self):
        """
        sqlmigrate outputs forward looking SQL.
        """
        with self.assertLogs('django.command') as logs:
            call_command("sqlmigrate", "migrations", "0001")
        output = combine_logs(logs).lower()

        index_tx_start = output.find(connection.ops.start_transaction_sql().lower())
        index_op_desc_author = output.find('-- create model author')
        index_create_table = output.find('create table')
        index_op_desc_tribble = output.find('-- create model tribble')
        index_op_desc_unique_together = output.find('-- alter unique_together')
        index_tx_end = output.find(connection.ops.end_transaction_sql().lower())

        if connection.features.can_rollback_ddl:
            self.assertGreater(index_tx_start, -1, "Transaction start not found")
            self.assertGreater(
                index_tx_end, index_op_desc_unique_together,
                "Transaction end not found or found before operation description (unique_together)"
            )

        self.assertGreater(
            index_op_desc_author, index_tx_start,
            "Operation description (author) not found or found before transaction start"
        )
        self.assertGreater(
            index_create_table, index_op_desc_author,
            "CREATE TABLE not found or found before operation description (author)"
        )
        self.assertGreater(
            index_op_desc_tribble, index_create_table,
            "Operation description (tribble) not found or found before CREATE TABLE (author)"
        )
        self.assertGreater(
            index_op_desc_unique_together, index_op_desc_tribble,
            "Operation description (unique_together) not found or found before operation description (tribble)"
        )

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations"})
    def test_sqlmigrate_backwards(self):
        """
        sqlmigrate outputs reverse looking SQL.
        """
        # Cannot generate the reverse SQL unless we've applied the migration.
        call_command("migrate", "migrations", verbosity=0)

        with self.assertLogs('django.command') as logs:
            call_command("sqlmigrate", "migrations", "0001", backwards=True)
        output = combine_logs(logs).lower()

        index_tx_start = output.find(connection.ops.start_transaction_sql().lower())
        index_op_desc_unique_together = output.find('-- alter unique_together')
        index_op_desc_tribble = output.find('-- create model tribble')
        index_op_desc_author = output.find('-- create model author')
        index_drop_table = output.rfind('drop table')
        index_tx_end = output.find(connection.ops.end_transaction_sql().lower())

        if connection.features.can_rollback_ddl:
            self.assertGreater(index_tx_start, -1, "Transaction start not found")
            self.assertGreater(
                index_tx_end, index_op_desc_unique_together,
                "Transaction end not found or found before DROP TABLE"
            )
        self.assertGreater(
            index_op_desc_unique_together, index_tx_start,
            "Operation description (unique_together) not found or found before transaction start"
        )
        self.assertGreater(
            index_op_desc_tribble, index_op_desc_unique_together,
            "Operation description (tribble) not found or found before operation description (unique_together)"
        )
        self.assertGreater(
            index_op_desc_author, index_op_desc_tribble,
            "Operation description (author) not found or found before operation description (tribble)"
        )

        self.assertGreater(
            index_drop_table, index_op_desc_author,
            "DROP TABLE not found or found before operation description (author)"
        )

        # Cleanup by unmigrating everything
        call_command("migrate", "migrations", "zero", verbosity=0)

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_non_atomic"})
    def test_sqlmigrate_for_non_atomic_migration(self):
        """
        Transaction wrappers aren't shown for non-atomic migrations.
        """
        with self.assertLogs('django.command') as logs:
            call_command("sqlmigrate", "migrations", "0001")
        output = combine_logs(logs).lower()
        queries = [q.strip() for q in output.splitlines()]
        if connection.ops.start_transaction_sql():
            self.assertNotIn(connection.ops.start_transaction_sql().lower(), queries)
        self.assertNotIn(connection.ops.end_transaction_sql().lower(), queries)

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations'})
    def test_sqlmigrate_for_non_transactional_databases(self):
        """
        Transaction wrappers aren't shown for databases that don't support
        transactional DDL.
        """
        with mock.patch.object(connection.features, 'can_rollback_ddl', False):
            with self.assertLogs('django.command') as logs:
                call_command('sqlmigrate', 'migrations', '0001')
        output = combine_logs(logs).lower()
        queries = [q.strip() for q in output.splitlines()]
        start_transaction_sql = connection.ops.start_transaction_sql()
        if start_transaction_sql:
            self.assertNotIn(start_transaction_sql.lower(), queries)
        self.assertNotIn(connection.ops.end_transaction_sql().lower(), queries)

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_squashed'})
    def test_sqlmigrate_ambiguous_prefix_squashed_migrations(self):
        with self.assertRaises(CommandError) as cm:
            call_command('sqlmigrate', 'migrations', '0001')
        [message] = cm.exception.args
        self.assertEqual(
            message,
            "More than one migration matches '%s' in app '%s'. Please be more specific."
        )
        self.assertEqual(cm.exception.logger_args, ('0001', 'migrations'))

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_squashed'})
    def test_sqlmigrate_squashed_migration(self):
        with self.assertLogs('django.command') as logs:
            call_command('sqlmigrate', 'migrations', '0001_squashed_0002')
            output = combine_logs(logs).lower()
        self.assertIn('-- create model author', output)
        self.assertIn('-- create model book', output)
        self.assertNotIn('-- create model tribble', output)

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_squashed'})
    def test_sqlmigrate_replaced_migration(self):
        with self.assertLogs('django.command') as logs:
            call_command('sqlmigrate', 'migrations', '0001_initial')
        output = combine_logs(logs).lower()
        self.assertIn('-- create model author', output)
        self.assertIn('-- create model tribble', output)

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations_no_operations'})
    def test_migrations_no_operations(self):
        with self.assertLogs('django.command', 'ERROR') as logs:
            call_command('sqlmigrate', 'migrations', '0001_initial')
        self.assertLogRecords(logs, [('ERROR', 'No operations found.', ())])

    @override_settings(
        INSTALLED_APPS=[
            "migrations.migrations_test_apps.migrated_app",
            "migrations.migrations_test_apps.migrated_unapplied_app",
            "migrations.migrations_test_apps.unmigrated_app",
        ],
    )
    def test_regression_22823_unmigrated_fk_to_migrated_model(self):
        """
        Assuming you have 3 apps, `A`, `B`, and `C`, such that:

        * `A` has migrations
        * `B` has a migration we want to apply
        * `C` has no migrations, but has an FK to `A`

        When we try to migrate "B", an exception occurs because the
        "B" was not included in the ProjectState that is used to detect
        soft-applied migrations (#22823).
        """
        call_command('migrate', 'migrated_unapplied_app', verbosity=0)

        # unmigrated_app.SillyModel has a foreign key to 'migrations.Tribble',
        # but that model is only defined in a migration, so the global app
        # registry never sees it and the reference is left dangling. Remove it
        # to avoid problems in subsequent tests.
        del apps._pending_operations[('migrations', 'tribble')]

    @override_settings(INSTALLED_APPS=['migrations.migrations_test_apps.unmigrated_app_syncdb'])
    def test_migrate_syncdb_deferred_sql_executed_with_schemaeditor(self):
        """
        For an app without migrations, editor.execute() is used for executing
        the syncdb deferred SQL.
        """
        with mock.patch.object(BaseDatabaseSchemaEditor, 'execute') as execute:
            with self.assertLogs('django.command', 'INFO') as logs:
                call_command('migrate', run_syncdb=True, verbosity=1, no_color=True)
            create_table_count = len([call for call in execute.mock_calls if 'CREATE TABLE' in str(call)])
            self.assertEqual(create_table_count, 2)
            # There's at least one deferred SQL for creating the foreign key
            # index.
            self.assertGreater(len(execute.mock_calls), 2)
        table_name = truncate_name('unmigrated_app_syncdb_classroom', connection.ops.max_name_length())
        self.assertLogRecords(logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Synchronize unmigrated apps: %s', ('unmigrated_app_syncdb',)),
            ('INFO', '  Apply all migrations: %s', ('(none)',)),
            ('INFO', 'Synchronizing apps without migrations:', ()),
            ('INFO', '  Creating tables...', ()),
            ('INFO', '    Creating table %s', (table_name,)),
            ('INFO', '    Creating table %s', ('unmigrated_app_syncdb_lesson',)),
            ('INFO', '    Running deferred SQL...', ()),
            ('INFO', 'Running migrations:', ()),
            ('INFO', '  No migrations to apply.', ()),
        ])

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations'})
    def test_migrate_syncdb_app_with_migrations(self):
        with self.assertRaises(CommandError) as cm:
            call_command('migrate', 'migrations', run_syncdb=True, verbosity=0)
        [message] = cm.exception.args
        self.assertEqual(message, "Can't use run_syncdb with app '%s' as it has migrations.")
        self.assertEqual(cm.exception.logger_args, ('migrations',))

    @override_settings(INSTALLED_APPS=[
        'migrations.migrations_test_apps.unmigrated_app_syncdb',
        'migrations.migrations_test_apps.unmigrated_app_simple',
    ])
    def test_migrate_syncdb_app_label(self):
        """
        Running migrate --run-syncdb with an app_label only creates tables for
        the specified app.
        """
        with mock.patch.object(BaseDatabaseSchemaEditor, 'execute') as execute:
            with self.assertLogs('django.command', 'INFO') as logs:
                call_command('migrate', 'unmigrated_app_syncdb', run_syncdb=True, no_color=True)
            create_table_count = len([call for call in execute.mock_calls if 'CREATE TABLE' in str(call)])
            self.assertEqual(create_table_count, 2)
            self.assertGreater(len(execute.mock_calls), 2)
        table_name = truncate_name('unmigrated_app_syncdb_classroom', connection.ops.max_name_length())
        self.assertLogRecords(logs, [
            ('INFO', 'Operations to perform:', ()),
            ('INFO', '  Synchronize unmigrated app: %s', ('unmigrated_app_syncdb',)),
            ('INFO', '  Apply all migrations: %s', ('(none)',)),
            ('INFO', 'Synchronizing apps without migrations:', ()),
            ('INFO', '  Creating tables...', ()),
            ('INFO', '    Creating table %s', (table_name,)),
            ('INFO', '    Creating table %s', ('unmigrated_app_syncdb_lesson',)),
            ('INFO', '    Running deferred SQL...', ()),
            ('INFO', 'Running migrations:', ()),
            ('INFO', '  No migrations to apply.', ()),
        ])

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_squashed"})
    def test_migrate_record_replaced(self):
        """
        Running a single squashed migration should record all of the original
        replaced migrations as run.
        """
        recorder = MigrationRecorder(connection)
        call_command("migrate", "migrations", verbosity=0)
        with self.assertLogs('django.command', 'INFO') as logs:
            call_command("showmigrations", "migrations", no_color=True)
        self.assertLogRecords(logs, [
            ('INFO', '%s', ('migrations',)),
            ('INFO', ' [X] %s', ('0001_squashed_0002 (2 squashed migrations)',)),
        ])
        applied_migrations = recorder.applied_migrations()
        self.assertIn(("migrations", "0001_initial"), applied_migrations)
        self.assertIn(("migrations", "0002_second"), applied_migrations)
        self.assertIn(("migrations", "0001_squashed_0002"), applied_migrations)
        # Rollback changes
        call_command("migrate", "migrations", "zero", verbosity=0)

    @override_settings(MIGRATION_MODULES={"migrations": "migrations.test_migrations_squashed"})
    def test_migrate_record_squashed(self):
        """
        Running migrate for a squashed migration should record as run
        if all of the replaced migrations have been run (#25231).
        """
        recorder = MigrationRecorder(connection)
        recorder.record_applied("migrations", "0001_initial")
        recorder.record_applied("migrations", "0002_second")
        call_command("migrate", "migrations", verbosity=0)
        with self.assertLogs('django.command', 'INFO') as logs:
            call_command("showmigrations", "migrations", no_color=True)
        self.assertLogRecords(logs, [
            ('INFO', '%s', ('migrations',)),
            ('INFO', ' [X] %s', ('0001_squashed_0002 (2 squashed migrations)',)),
        ])
        self.assertIn(
            ("migrations", "0001_squashed_0002"),
            recorder.applied_migrations()
        )
        # No changes were actually applied so there is nothing to rollback

    @override_settings(MIGRATION_MODULES={'migrations': 'migrations.test_migrations'})
    def test_migrate_inconsistent_history(self):
        """
        Running migrate with some migrations applied before their dependencies
        should not be allowed.
        """
        recorder = MigrationRecorder(connection)
        recorder.record_applied("migrations", "0002_second")
        msg = "Migration migrations.0002_second is applied before its dependency migrations.0001_initial"
        with self.assertRaisesMessage(InconsistentMigrationHistory, msg):
            call_command("migrate")
        applied_migrations = recorder.applied_migrations()
        self.assertNotIn(("migrations", "0001_initial"), applied_migrations)

    @override_settings(INSTALLED_APPS=[
        'migrations.migrations_test_apps.migrated_unapplied_app',
        'migrations.migrations_test_apps.migrated_app',
    ])
    def test_migrate_not_reflected_changes(self):
        class NewModel1(models.Model):
            class Meta():
                app_label = 'migrated_app'

        class NewModel2(models.Model):
            class Meta():
                app_label = 'migrated_unapplied_app'

        try:
            call_command('migrate', verbosity=0)
            with self.assertLogs('django.command') as logs:
                call_command('migrate', no_color=True)
            self.assertLogRecords(
                logs,
                [
                    ('INFO', "Operations to perform:", ()),
                    ('INFO',
                     '  Apply all migrations: %s, %s',
                     ('migrated_app', 'migrated_unapplied_app')),
                    ('INFO', "Running migrations:", ()),
                    ('INFO', "  No migrations to apply.", ()),
                    (
                        'INFO',
                        '  Your models in app(s): %s, '
                        '%s have changes that are not yet reflected in a migration, '
                        "and so won't be applied.",
                        ("'migrated_app'", "'migrated_unapplied_app'")),
                    (
                        'INFO',
                        "  Run 'manage.py makemigrations' to make new migrations, and then re-run "
                        "'manage.py migrate' to apply them.",
                        ()),
                ]
            )
        finally:
            # Unmigrate everything.
            call_command('migrate', 'migrated_app', 'zero', verbosity=0)
            call_command('migrate', 'migrated_unapplied_app', 'zero', verbosity=0)


class MakeMigrationsTests(MigrationTestBase):
    """
    Tests running the makemigrations command.
    """

    def setUp(self):
        super().setUp()
        self._old_models = apps.app_configs['migrations'].models.copy()

    def tearDown(self):
        apps.app_configs['migrations'].models = self._old_models
        apps.all_models['migrations'] = self._old_models
        apps.clear_cache()
        super().tearDown()

    def test_files_content(self):
        self.assertTableNotExists("migrations_unicodemodel")
        apps.register_model('migrations', UnicodeModel)
        with self.temporary_migration_module() as migration_dir:
            call_command("makemigrations", "migrations", verbosity=0)

            # Check for empty __init__.py file in migrations folder
            init_file = os.path.join(migration_dir, "__init__.py")
            self.assertTrue(os.path.exists(init_file))

            with open(init_file) as fp:
                content = fp.read()
            self.assertEqual(content, '')

            # Check for existing 0001_initial.py file in migration folder
            initial_file = os.path.join(migration_dir, "0001_initial.py")
            self.assertTrue(os.path.exists(initial_file))

            with open(initial_file, encoding='utf-8') as fp:
                content = fp.read()
                self.assertIn('migrations.CreateModel', content)
                self.assertIn('initial = True', content)

                self.assertIn('úñí©óðé µóðéø', content)  # Meta.verbose_name
                self.assertIn('úñí©óðé µóðéøß', content)  # Meta.verbose_name_plural
                self.assertIn('ÚÑÍ¢ÓÐÉ', content)  # title.verbose_name
                self.assertIn('“Ðjáñgó”', content)  # title.default

    def test_makemigrations_order(self):
        """
        makemigrations should recognize number-only migrations (0001.py).
        """
        module = 'migrations.test_migrations_order'
        with self.temporary_migration_module(module=module) as migration_dir:
            if hasattr(importlib, 'invalidate_caches'):
                # importlib caches os.listdir() on some platforms like macOS
                # (#23850).
                importlib.invalidate_caches()
            call_command('makemigrations', 'migrations', '--empty', '-n', 'a', '-v', '0')
            self.assertTrue(os.path.exists(os.path.join(migration_dir, '0002_a.py')))

    def test_makemigrations_empty_connections(self):
        empty_connections = ConnectionHandler({'default': {}})
        with mock.patch('django.core.management.commands.makemigrations.connections', new=empty_connections):
            # with no apps
            with self.assertLogs('django.command') as logs:
                call_command('makemigrations')
            self.assertLogRecords(logs, [('INFO', 'No changes detected', ())])
            # with an app
            with self.temporary_migration_module() as migration_dir:
                call_command('makemigrations', 'migrations', verbosity=0)
                init_file = os.path.join(migration_dir, '__init__.py')
                self.assertTrue(os.path.exists(init_file))

    @override_settings(INSTALLED_APPS=['migrations', 'migrations2'])
    def test_makemigrations_consistency_checks_respect_routers(self):
        """
        The history consistency checks in makemigrations respect
        settings.DATABASE_ROUTERS.
        """
        def patched_has_table(migration_recorder):
            if migration_recorder.connection is connections['other']:
                raise Exception('Other connection')
            else:
                return mock.DEFAULT

        self.assertTableNotExists('migrations_unicodemodel')
        apps.register_model('migrations', UnicodeModel)
        with mock.patch.object(
                MigrationRecorder, 'has_table',
                autospec=True, side_effect=patched_has_table) as has_table:
            with self.temporary_migration_module() as migration_dir:
                call_command("makemigrations", "migrations", verbosity=0)
                initial_file = os.path.join(migration_dir, "0001_initial.py")
                self.assertTrue(os.path.exists(initial_file))
                self.assertEqual(has_table.call_count, 1)  # 'default' is checked

                # Router says not to migrate 'other' so consistency shouldn't
                # be checked.
                with self.settings(DATABASE_ROUTERS=['migrations.routers.TestRouter']):
                    call_command('makemigrations', 'migrations', verbosity=0)
                self.assertEqual(has_table.call_count, 2)  # 'default' again

                # With a router that doesn't prohibit migrating 'other',
                # consistency is checked.
                with self.settings(DATABASE_ROUTERS=['migrations.routers.DefaultOtherRouter']):
                    with self.assertRaisesMessage(Exception, 'Other connection'):
                        call_command('makemigrations', 'migrations', verbosity=0)
                self.assertEqual(has_table.call_count, 4)  # 'default' and 'other'

                # With a router that doesn't allow migrating on any database,
                # no consistency checks are made.
                with self.settings(DATABASE_ROUTERS=['migrations.routers.TestRouter']):
                    with mock.patch.object(TestRouter, 'allow_migrate', return_value=False) as allow_migrate:
                        call_command('makemigrations', 'migrations', verbosity=0)
                allow_migrate.assert_any_call('other', 'migrations', model_name='UnicodeModel')
                # allow_migrate() is called with the correct arguments.
                self.assertGreater(len(allow_migrate.mock_calls), 0)
                called_aliases = set()
                for mock_call in allow_migrate.mock_calls:
                    _, call_args, call_kwargs = mock_call
                    connection_alias, app_name = call_args
                    called_aliases.add(connection_alias)
                    # Raises an error if invalid app_name/model_name occurs.
                    apps.get_app_config(app_name).get_model(call_kwargs['model_name'])
                self.assertEqual(called_aliases, set(connections))
                self.assertEqual(has_table.call_count, 4)

    def test_failing_migration(self):
        # If a migration fails to serialize, it shouldn't generate an empty file. #21280
        apps.register_model('migrations', UnserializableModel)

        with self.temporary_migration_module() as migration_dir:
            with self.assertRaisesMessage(ValueError, 'Cannot serialize'):
                call_command("makemigrations", "migrations", verbosity=0)

            initial_file = os.path.join(migration_dir, "0001_initial.py")
            self.assertFalse(os.path.exists(initial_file))

    def test_makemigrations_conflict_exit(self):
        """
        makemigrations exits if it detects a conflict.
        """
        with self.temporary_migration_module(module="migrations.test_migrations_conflict"):
            with self.assertRaises(CommandError) as context:
                call_command("makemigrations")
        [message] = context.exception.args
        self.assertEqual(
            message,
            'Conflicting migrations detected; multiple leaf nodes in the '
            'migration graph: (%s, %s in %s).\n'
            "To fix them run 'python manage.py makemigrations --merge'"
        )
        self.assertEqual(
            context.exception.logger_args,
            ('0002_conflicting_second', '0002_second', 'migrations')
        )

    def test_makemigrations_merge_no_conflict(self):
        """
        makemigrations exits if in merge mode with no conflicts.
        """
        with self.temporary_migration_module(
            module="migrations.test_migrations",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", merge=True)
        self.assertLogRecords(logs, [('INFO', 'No conflicts detected to merge.', ())])

    def test_makemigrations_empty_no_app_specified(self):
        """
        makemigrations exits if no app is specified with 'empty' mode.
        """
        msg = 'You must supply at least one app label when using --empty.'
        with self.assertRaisesMessage(CommandError, msg):
            call_command("makemigrations", empty=True)

    def test_makemigrations_empty_migration(self):
        """
        makemigrations properly constructs an empty migration.
        """
        with self.temporary_migration_module() as migration_dir:
            call_command("makemigrations", "migrations", empty=True, verbosity=0)

            # Check for existing 0001_initial.py file in migration folder
            initial_file = os.path.join(migration_dir, "0001_initial.py")
            self.assertTrue(os.path.exists(initial_file))

            with open(initial_file, encoding='utf-8') as fp:
                content = fp.read()

                # Remove all whitespace to check for empty dependencies and operations
                content = content.replace(' ', '')
                self.assertIn('dependencies=[\n]', content)
                self.assertIn('operations=[\n]', content)

    @override_settings(MIGRATION_MODULES={"migrations": None})
    def test_makemigrations_disabled_migrations_for_app(self):
        """
        makemigrations raises a nice error when migrations are disabled for an
        app.
        """
        msg = (
            "Django can't create migrations for app 'migrations' because migrations "
            "have been disabled via the MIGRATION_MODULES setting."
        )
        with self.assertRaisesMessage(ValueError, msg):
            call_command("makemigrations", "migrations", empty=True, verbosity=0)

    def test_makemigrations_no_changes_no_apps(self):
        """
        makemigrations exits when there are no changes and no apps are specified.
        """
        with self.assertLogs('django.command') as logs:
            call_command("makemigrations")
        self.assertLogRecords(logs, [('INFO', 'No changes detected', ())])

    def test_makemigrations_no_changes(self):
        """
        makemigrations exits when there are no changes to an app.
        """
        with self.temporary_migration_module(
                module="migrations.test_migrations_no_changes",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations")
        self.assertLogRecords(logs, [('INFO', "No changes detected in app '%s'", ('migrations',))])

    def test_makemigrations_no_apps_initial(self):
        """
        makemigrations should detect initial is needed on empty migration
        modules if no app provided.
        """
        with self.temporary_migration_module(
                module="migrations.test_migrations_empty",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations")
        self.assertIn("0001_initial.py", combine_logs(logs))

    def test_makemigrations_no_init(self):
        """Migration directories without an __init__.py file are allowed."""
        with self.temporary_migration_module(
                module='migrations.test_migrations_no_init',
        ), self.assertLogs('django.command') as logs:
            call_command('makemigrations')
        self.assertIn('0001_initial.py', combine_logs(logs))

    def test_makemigrations_migrations_announce(self):
        """
        makemigrations announces the migration at the default verbosity level.
        """
        with self.temporary_migration_module(), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations")
        self.assertIn("Migrations for 'migrations'", combine_logs(logs))

    def test_makemigrations_no_common_ancestor(self):
        """
        makemigrations fails to merge migrations with no common ancestor.
        """
        with self.assertRaises(ValueError) as context:
            with self.temporary_migration_module(module="migrations.test_migrations_no_ancestor"):
                call_command("makemigrations", "migrations", merge=True)
        exception_message = str(context.exception)
        self.assertIn("Could not find common ancestor of", exception_message)
        self.assertIn("0002_second", exception_message)
        self.assertIn("0002_conflicting_second", exception_message)

    def test_makemigrations_interactive_reject(self):
        """
        makemigrations enters and exits interactive mode properly.
        """
        # Monkeypatch interactive questioner to auto reject
        with mock.patch('builtins.input', mock.Mock(return_value='N')):
            with self.temporary_migration_module(module="migrations.test_migrations_conflict") as migration_dir:
                call_command("makemigrations", "migrations", name="merge", merge=True, interactive=True, verbosity=0)
                merge_file = os.path.join(migration_dir, '0003_merge.py')
                self.assertFalse(os.path.exists(merge_file))

    def test_makemigrations_interactive_accept(self):
        """
        makemigrations enters interactive mode and merges properly.
        """
        # Monkeypatch interactive questioner to auto accept
        with mock.patch('builtins.input', mock.Mock(return_value='y')):
            with self.temporary_migration_module(module="migrations.test_migrations_conflict") as migration_dir:
                with self.assertLogs('django.command') as logs:
                    call_command("makemigrations", "migrations", name="merge", merge=True, interactive=True)
                merge_file = os.path.join(migration_dir, '0003_merge.py')
                self.assertTrue(os.path.exists(merge_file))
            self.assertIn("Created new merge migration", combine_logs(logs))

    def test_makemigrations_default_merge_name(self):
        with self.temporary_migration_module(
            module='migrations.test_migrations_conflict'
        ) as migration_dir, self.assertLogs('django.command') as logs:
            call_command('makemigrations', 'migrations', merge=True, interactive=False, no_color=True)
            merge_file = os.path.join(
                migration_dir,
                '0003_merge_0002_conflicting_second_0002_second.py',
            )
            self.assertIs(os.path.exists(merge_file), True)
        self.assertLogRecords(
            logs,
            [
                ('INFO', 'Merging %s', ('migrations',)),
                ('INFO', '  Branch %s', ('0002_conflicting_second',)),
                ('INFO', '    - %s', ('Create model Something',)),
                ('INFO', '  Branch %s', ('0002_second',)),
                ('INFO', '    - %s', ('Delete model Tribble',)),
                ('INFO', '    - %s', ('Remove field silly_field from Author',)),
                ('INFO', '    - %s', ('Add field rating to Author',)),
                ('INFO', '    - %s', ('Create model Book',)),
                ('INFO', '\nCreated new merge migration %s', (merge_file,)),
            ]
        )

    @mock.patch('django.db.migrations.utils.datetime')
    def test_makemigrations_auto_merge_name(self, mock_datetime):
        mock_datetime.datetime.now.return_value = datetime.datetime(2016, 1, 2, 3, 4)
        with mock.patch('builtins.input', mock.Mock(return_value='y')):
            with self.temporary_migration_module(
                module='migrations.test_migrations_conflict_long_name'
            ) as migration_dir, self.assertLogs('django.command') as logs:
                call_command("makemigrations", "migrations", merge=True, interactive=True)
                merge_file = os.path.join(migration_dir, '0003_merge_20160102_0304.py')
                self.assertTrue(os.path.exists(merge_file))
            self.assertIn("Created new merge migration", combine_logs(logs))

    def test_makemigrations_non_interactive_not_null_addition(self):
        """
        Non-interactive makemigrations fails when a default is missing on a
        new not-null field.
        """
        class SillyModel(models.Model):
            silly_field = models.BooleanField(default=False)
            silly_int = models.IntegerField()

            class Meta:
                app_label = "migrations"

        with self.assertRaises(SystemExit):
            with self.temporary_migration_module(module="migrations.test_migrations_no_default"):
                call_command("makemigrations", "migrations", interactive=False)

    def test_makemigrations_non_interactive_not_null_alteration(self):
        """
        Non-interactive makemigrations fails when a default is missing on a
        field changed to not-null.
        """
        class Author(models.Model):
            name = models.CharField(max_length=255)
            slug = models.SlugField()
            age = models.IntegerField(default=0)

            class Meta:
                app_label = "migrations"

        with self.temporary_migration_module(
                module="migrations.test_migrations",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations", interactive=False)
        self.assertIn("Alter field slug on author", combine_logs(logs))

    def test_makemigrations_non_interactive_no_model_rename(self):
        """
        makemigrations adds and removes a possible model rename in
        non-interactive mode.
        """
        class RenamedModel(models.Model):
            silly_field = models.BooleanField(default=False)

            class Meta:
                app_label = "migrations"

        with self.temporary_migration_module(
                module="migrations.test_migrations_no_default",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations", interactive=False)
        output = combine_logs(logs)
        self.assertIn("Delete model SillyModel", output)
        self.assertIn("Create model RenamedModel", output)

    def test_makemigrations_non_interactive_no_field_rename(self):
        """
        makemigrations adds and removes a possible field rename in
        non-interactive mode.
        """
        class SillyModel(models.Model):
            silly_rename = models.BooleanField(default=False)

            class Meta:
                app_label = "migrations"

        with self.temporary_migration_module(
                module="migrations.test_migrations_no_default",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations", interactive=False)
        output = combine_logs(logs)
        self.assertIn("Remove field silly_field from sillymodel", output)
        self.assertIn("Add field silly_rename to sillymodel", output)

    def test_makemigrations_handle_merge(self):
        """
        makemigrations properly merges the conflicting migrations with --noinput.
        """
        with self.temporary_migration_module(module="migrations.test_migrations_conflict") as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command("makemigrations", "migrations", name="merge", merge=True, interactive=False)
            merge_file = os.path.join(migration_dir, '0003_merge.py')
            self.assertTrue(os.path.exists(merge_file))
        output = combine_logs(logs)
        self.assertIn("Merging migrations", output)
        self.assertIn("Branch 0002_second", output)
        self.assertIn("Branch 0002_conflicting_second", output)
        self.assertIn("Created new merge migration", output)

    def test_makemigration_merge_dry_run(self):
        """
        makemigrations respects --dry-run option when fixing migration
        conflicts (#24427).
        """
        with self.temporary_migration_module(module="migrations.test_migrations_conflict") as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command(
                    "makemigrations", "migrations", name="merge", dry_run=True,
                    merge=True, interactive=False
                )
            merge_file = os.path.join(migration_dir, '0003_merge.py')
            self.assertFalse(os.path.exists(merge_file))
        output = combine_logs(logs)
        self.assertIn("Merging migrations", output)
        self.assertIn("Branch 0002_second", output)
        self.assertIn("Branch 0002_conflicting_second", output)
        self.assertNotIn("Created new merge migration", output)

    def test_makemigration_merge_dry_run_verbosity_3(self):
        """
        `makemigrations --merge --dry-run` writes the merge migration file to
        stdout with `verbosity == 3` (#24427).
        """
        with self.temporary_migration_module(module="migrations.test_migrations_conflict") as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command(
                    "makemigrations", "migrations", name="merge", dry_run=True,
                    merge=True, interactive=False, verbosity=3,
                )
            merge_file = os.path.join(migration_dir, '0003_merge.py')
            self.assertFalse(os.path.exists(merge_file))
        output = combine_logs(logs)
        self.assertIn("Merging migrations", output)
        self.assertIn("Branch 0002_second", output)
        self.assertIn("Branch 0002_conflicting_second", output)
        self.assertNotIn("Created new merge migration", output)

        # Additional output caused by verbosity 3
        # The complete merge migration file that would be written
        self.assertIn("class Migration(migrations.Migration):", output)
        self.assertIn("dependencies = [", output)
        self.assertIn("('migrations', '0002_second')", output)
        self.assertIn("('migrations', '0002_conflicting_second')", output)
        self.assertIn("operations = [", output)
        self.assertIn("]", output)

    def test_makemigrations_dry_run(self):
        """
        `makemigrations --dry-run` should not ask for defaults.
        """
        class SillyModel(models.Model):
            silly_field = models.BooleanField(default=False)
            silly_date = models.DateField()  # Added field without a default

            class Meta:
                app_label = "migrations"

        with self.temporary_migration_module(
                module="migrations.test_migrations_no_default",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations", dry_run=True)
        # Output the expected changes directly, without asking for defaults
        self.assertIn("Add field silly_date to sillymodel", combine_logs(logs))

    def test_makemigrations_dry_run_verbosity_3(self):
        """
        Allow `makemigrations --dry-run` to output the migrations file to
        stdout (with verbosity == 3).
        """
        class SillyModel(models.Model):
            silly_field = models.BooleanField(default=False)
            silly_char = models.CharField(default="")

            class Meta:
                app_label = "migrations"

        with self.temporary_migration_module(
                module="migrations.test_migrations_no_default",
        ), self.assertLogs('django.command') as logs:
            call_command("makemigrations", "migrations", dry_run=True, verbosity=3)

        output = combine_logs(logs)
        # Normal --dry-run output
        self.assertIn("- Add field silly_char to sillymodel", output)

        # Additional output caused by verbosity 3
        # The complete migrations file that would be written
        self.assertIn("class Migration(migrations.Migration):", output)
        self.assertIn("dependencies = [", output)
        self.assertIn("('migrations', '0001_initial'),", output)
        self.assertIn("migrations.AddField(", output)
        self.assertIn("model_name='sillymodel',", output)
        self.assertIn("name='silly_char',", output)

    def test_makemigrations_migrations_modules_path_not_exist(self):
        """
        makemigrations creates migrations when specifying a custom location
        for migration files using MIGRATION_MODULES if the custom path
        doesn't already exist.
        """
        class SillyModel(models.Model):
            silly_field = models.BooleanField(default=False)

            class Meta:
                app_label = "migrations"

        migration_module = "migrations.test_migrations_path_doesnt_exist.foo.bar"
        with self.temporary_migration_module(module=migration_module) as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command("makemigrations", "migrations")

            # Migrations file is actually created in the expected path.
            initial_file = os.path.join(migration_dir, "0001_initial.py")
            self.assertTrue(os.path.exists(initial_file))

        # Command output indicates the migration is created.
        self.assertIn(" - Create model SillyModel", combine_logs(logs))

    @override_settings(MIGRATION_MODULES={'migrations': 'some.nonexistent.path'})
    def test_makemigrations_migrations_modules_nonexistent_toplevel_package(self):
        msg = (
            'Could not locate an appropriate location to create migrations '
            'package some.nonexistent.path. Make sure the toplevel package '
            'exists and can be imported.'
        )
        with self.assertRaisesMessage(ValueError, msg):
            call_command('makemigrations', 'migrations', empty=True, verbosity=0)

    def test_makemigrations_interactive_by_default(self):
        """
        The user is prompted to merge by default if there are conflicts and
        merge is True. Answer negative to differentiate it from behavior when
        --noinput is specified.
        """
        # Monkeypatch interactive questioner to auto reject
        with mock.patch('builtins.input', mock.Mock(return_value='N')):
            with self.temporary_migration_module(module="migrations.test_migrations_conflict") as migration_dir:
                with self.assertLogs('django.command') as logs:
                    call_command("makemigrations", "migrations", name="merge", merge=True)
                merge_file = os.path.join(migration_dir, '0003_merge.py')
                # This will fail if interactive is False by default
                self.assertFalse(os.path.exists(merge_file))
            self.assertNotIn("Created new merge migration", combine_logs(logs))

    @override_settings(
        INSTALLED_APPS=[
            "migrations",
            "migrations.migrations_test_apps.unspecified_app_with_conflict"])
    def test_makemigrations_unspecified_app_with_conflict_no_merge(self):
        """
        makemigrations does not raise a CommandError when an unspecified app
        has conflicting migrations.
        """
        with self.temporary_migration_module(module="migrations.test_migrations_no_changes"):
            call_command("makemigrations", "migrations", merge=False, verbosity=0)

    @override_settings(
        INSTALLED_APPS=[
            "migrations.migrations_test_apps.migrated_app",
            "migrations.migrations_test_apps.unspecified_app_with_conflict"])
    def test_makemigrations_unspecified_app_with_conflict_merge(self):
        """
        makemigrations does not create a merge for an unspecified app even if
        it has conflicting migrations.
        """
        # Monkeypatch interactive questioner to auto accept
        with mock.patch('builtins.input', mock.Mock(return_value='y')):
            with self.temporary_migration_module(app_label="migrated_app") as migration_dir:
                with self.assertLogs('django.command') as logs:
                    call_command("makemigrations", "migrated_app", name="merge", merge=True, interactive=True)
                merge_file = os.path.join(migration_dir, '0003_merge.py')
                self.assertFalse(os.path.exists(merge_file))
            self.assertIn("No conflicts detected to merge.", combine_logs(logs))

    @override_settings(
        INSTALLED_APPS=[
            "migrations.migrations_test_apps.migrated_app",
            "migrations.migrations_test_apps.conflicting_app_with_dependencies"])
    def test_makemigrations_merge_dont_output_dependency_operations(self):
        """
        makemigrations --merge does not output any operations from apps that
        don't belong to a given app.
        """
        # Monkeypatch interactive questioner to auto accept
        with mock.patch('builtins.input', mock.Mock(return_value='N')):
            with mock.patch(
                'django.core.management.color.supports_color', lambda *args: False
            ), self.assertLogs('django.command') as logs:
                call_command(
                    "makemigrations", "conflicting_app_with_dependencies",
                    merge=True, interactive=True,
                )
            self.assertLogRecords(
                logs,
                [
                    ('INFO', 'Merging %s', ('conflicting_app_with_dependencies',)),
                    ('INFO', '  Branch %s', ('0002_conflicting_second',)),
                    ('INFO', '    - %s', ('Create model Something',)),
                    ('INFO', '  Branch %s', ('0002_second',)),
                    ('INFO', '    - %s', ('Delete model Tribble',)),
                    ('INFO', '    - %s', ('Remove field silly_field from Author',)),
                    ('INFO', '    - %s', ('Add field rating to Author',)),
                    ('INFO', '    - %s', ('Create model Book',)),
                ],
            )

    def test_makemigrations_with_custom_name(self):
        """
        makemigrations --name generate a custom migration name.
        """
        with self.temporary_migration_module() as migration_dir:

            def cmd(migration_count, migration_name, *args):
                call_command("makemigrations", "migrations", "--verbosity", "0", "--name", migration_name, *args)
                migration_file = os.path.join(migration_dir, "%s_%s.py" % (migration_count, migration_name))
                # Check for existing migration file in migration folder
                self.assertTrue(os.path.exists(migration_file))
                with open(migration_file, encoding='utf-8') as fp:
                    content = fp.read()
                    content = content.replace(" ", "")
                return content

            # generate an initial migration
            migration_name_0001 = "my_initial_migration"
            content = cmd("0001", migration_name_0001)
            self.assertIn("dependencies=[\n]", content)

            # importlib caches os.listdir() on some platforms like macOS
            # (#23850).
            if hasattr(importlib, 'invalidate_caches'):
                importlib.invalidate_caches()

            # generate an empty migration
            migration_name_0002 = "my_custom_migration"
            content = cmd("0002", migration_name_0002, "--empty")
            self.assertIn("dependencies=[\n('migrations','0001_%s'),\n]" % migration_name_0001, content)
            self.assertIn("operations=[\n]", content)

    def test_makemigrations_with_invalid_custom_name(self):
        msg = 'The migration name must be a valid Python identifier.'
        with self.assertRaisesMessage(CommandError, msg):
            call_command('makemigrations', 'migrations', '--name', 'invalid name', '--empty')

    def test_makemigrations_check(self):
        """
        makemigrations --check should exit with a non-zero status when
        there are changes to an app requiring migrations.
        """
        with self.temporary_migration_module():
            with self.assertRaises(SystemExit):
                call_command("makemigrations", "--check", "migrations", verbosity=0)

        with self.temporary_migration_module(module="migrations.test_migrations_no_changes"):
            call_command("makemigrations", "--check", "migrations", verbosity=0)

    def test_makemigrations_migration_path_output(self):
        """
        makemigrations should print the relative paths to the migrations unless
        they are outside of the current tree, in which case the absolute path
        should be shown.
        """
        apps.register_model('migrations', UnicodeModel)
        with self.temporary_migration_module() as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command("makemigrations", "migrations")
            self.assertIn(os.path.join(migration_dir, '0001_initial.py'), combine_logs(logs))

    def test_makemigrations_migration_path_output_valueerror(self):
        """
        makemigrations prints the absolute path if os.path.relpath() raises a
        ValueError when it's impossible to obtain a relative path, e.g. on
        Windows if Django is installed on a different drive than where the
        migration files are created.
        """
        with self.temporary_migration_module() as migration_dir:
            with mock.patch('os.path.relpath', side_effect=ValueError):
                with self.assertLogs('django.command') as logs:
                    call_command('makemigrations', 'migrations')
        self.assertIn(os.path.join(migration_dir, '0001_initial.py'), combine_logs(logs))

    def test_makemigrations_inconsistent_history(self):
        """
        makemigrations should raise InconsistentMigrationHistory exception if
        there are some migrations applied before their dependencies.
        """
        recorder = MigrationRecorder(connection)
        recorder.record_applied('migrations', '0002_second')
        msg = "Migration migrations.0002_second is applied before its dependency migrations.0001_initial"
        with self.temporary_migration_module(module="migrations.test_migrations"):
            with self.assertRaisesMessage(InconsistentMigrationHistory, msg):
                call_command("makemigrations")

    def test_makemigrations_inconsistent_history_db_failure(self):
        msg = (
            "Got an error checking a consistent migration history performed "
            "for database connection 'default': could not connect to server"
        )
        with mock.patch(
            'django.db.migrations.loader.MigrationLoader.check_consistent_history',
            side_effect=OperationalError('could not connect to server'),
        ):
            with self.temporary_migration_module():
                with self.assertWarns(RuntimeWarning) as cm:
                    call_command('makemigrations', verbosity=0)
                self.assertEqual(str(cm.warning), msg)

    @mock.patch('builtins.input', return_value='1')
    @mock.patch('django.db.migrations.questioner.sys.stdin', mock.MagicMock(encoding=sys.getdefaultencoding()))
    def test_makemigrations_auto_now_add_interactive(self, *args):
        """
        makemigrations prompts the user when adding auto_now_add to an existing
        model.
        """
        class Entry(models.Model):
            title = models.CharField(max_length=255)
            creation_date = models.DateTimeField(auto_now_add=True)

            class Meta:
                app_label = 'migrations'

        # Monkeypatch interactive questioner to auto accept
        with mock.patch('django.db.migrations.questioner.sys.stdout', new_callable=io.StringIO) as prompt_stdout:
            with self.temporary_migration_module(module='migrations.test_auto_now_add'):
                with self.assertLogs('django.command') as logs:
                    call_command('makemigrations', 'migrations', interactive=True)
            output = combine_logs(logs)
            prompt_output = prompt_stdout.getvalue()
            self.assertIn("You can accept the default 'timezone.now' by pressing 'Enter'", prompt_output)
            self.assertIn("Add field creation_date to entry", output)


class SquashMigrationsTests(MigrationTestBase):
    """
    Tests running the squashmigrations command.
    """

    def test_squashmigrations_squashes(self):
        """
        squashmigrations squashes migrations.
        """
        with self.temporary_migration_module(module="migrations.test_migrations") as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command('squashmigrations', 'migrations', '0002', interactive=False, no_color=True)

            squashed_migration_file = os.path.join(migration_dir, "0001_squashed_0002_second.py")
            self.assertTrue(os.path.exists(squashed_migration_file))
        self.assertLogRecords(
            logs,
            [('INFO', 'Will squash the following migrations:', ()),
             ('INFO', ' - %s', ('0001_initial',)),
             ('INFO', ' - %s', ('0002_second',)),
             ('INFO', 'Optimizing...', ()),
             ('INFO', '  Optimized from %s operations to %s operations.', (8, 2)),
             ('INFO',
              'Created new squashed migration %s\n'
              '  You should commit this migration but leave the old ones in place;\n'
              '  the new migration will be used for new installs. Once you are sure\n'
              '  all instances of the codebase have applied the migrations you squashed,\n'
              '  you can delete them.',
              (squashed_migration_file,))]
        )

    def test_squashmigrations_initial_attribute(self):
        with self.temporary_migration_module(module="migrations.test_migrations") as migration_dir:
            call_command("squashmigrations", "migrations", "0002", interactive=False, verbosity=0)

            squashed_migration_file = os.path.join(migration_dir, "0001_squashed_0002_second.py")
            with open(squashed_migration_file, encoding='utf-8') as fp:
                content = fp.read()
                self.assertIn("initial = True", content)

    def test_squashmigrations_optimizes(self):
        """
        squashmigrations optimizes operations.
        """
        with self.temporary_migration_module(module="migrations.test_migrations"):
            with self.assertLogs('django.command') as logs:
                call_command("squashmigrations", "migrations", "0002", interactive=False, verbosity=1)
        self.assertIn("Optimized from 8 operations to 2 operations.", combine_logs(logs))

    def test_ticket_23799_squashmigrations_no_optimize(self):
        """
        squashmigrations --no-optimize doesn't optimize operations.
        """
        with self.temporary_migration_module(module="migrations.test_migrations"):
            with self.assertLogs('django.command') as logs:
                call_command("squashmigrations", "migrations", "0002",
                             interactive=False, verbosity=1, no_optimize=True)
        self.assertIn("Skipping optimization", combine_logs(logs))

    def test_squashmigrations_valid_start(self):
        """
        squashmigrations accepts a starting migration.
        """
        with self.temporary_migration_module(module="migrations.test_migrations_no_changes") as migration_dir:
            with self.assertLogs('django.command') as logs:
                call_command("squashmigrations", "migrations", "0002", "0003",
                             interactive=False, verbosity=1)

            squashed_migration_file = os.path.join(migration_dir, "0002_second_squashed_0003_third.py")
            with open(squashed_migration_file, encoding='utf-8') as fp:
                content = fp.read()
                self.assertIn("        ('migrations', '0001_initial')", content)
                self.assertNotIn("initial = True", content)
        out = combine_logs(logs)
        self.assertNotIn(" - 0001_initial", out)
        self.assertIn(" - 0002_second", out)
        self.assertIn(" - 0003_third", out)

    def test_squashmigrations_invalid_start(self):
        """
        squashmigrations doesn't accept a starting migration after the ending migration.
        """
        with self.temporary_migration_module(
            module="migrations.test_migrations_no_changes"
        ), self.assertRaises(CommandError) as cm:
            call_command("squashmigrations", "migrations", "0003", "0002", interactive=False, verbosity=0)
        [message] = cm.exception.args
        self.assertEqual(
            message,
            "The migration '%s' cannot be found. Maybe it comes after the migration '%s'?\n"
            'Have a look at:\n'
            '  python manage.py showmigrations %s\n'
            'to debug this issue.'
        )
        self.assertEqual(
            cm.exception.logger_args,
            ('migrations.0003_third', 'migrations.0002_second', 'migrations')
        )

    def test_squashed_name_with_start_migration_name(self):
        """--squashed-name specifies the new migration's name."""
        squashed_name = 'squashed_name'
        with self.temporary_migration_module(module='migrations.test_migrations') as migration_dir:
            call_command(
                'squashmigrations', 'migrations', '0001', '0002',
                squashed_name=squashed_name, interactive=False, verbosity=0,
            )
            squashed_migration_file = os.path.join(migration_dir, '0001_%s.py' % squashed_name)
            self.assertTrue(os.path.exists(squashed_migration_file))

    def test_squashed_name_without_start_migration_name(self):
        """--squashed-name also works if a start migration is omitted."""
        squashed_name = 'squashed_name'
        with self.temporary_migration_module(module="migrations.test_migrations") as migration_dir:
            call_command(
                'squashmigrations', 'migrations', '0001',
                squashed_name=squashed_name, interactive=False, verbosity=0,
            )
            squashed_migration_file = os.path.join(migration_dir, '0001_%s.py' % squashed_name)
            self.assertTrue(os.path.exists(squashed_migration_file))


class AppLabelErrorTests(TestCase):
    """
    This class inherits TestCase because MigrationTestBase uses
    `available_apps = ['migrations']` which means that it's the only installed
    app. 'django.contrib.auth' must be in INSTALLED_APPS for some of these
    tests.
    """
    nonexistent_app_error = "No installed app with label 'nonexistent_app'."
    did_you_mean_auth_error = (
        "No installed app with label 'django.contrib.auth'. Did you mean "
        "'auth'?"
    )

    def test_makemigrations_nonexistent_app_label(self):
        with self.assertRaises(SystemExit), self.assertLogs('django.command', 'ERROR') as logs:
            call_command('makemigrations', 'nonexistent_app')
        self.assertLogRecords(logs, [('ERROR', self.nonexistent_app_error, ())])

    def test_makemigrations_app_name_specified_as_label(self):
        with self.assertRaises(SystemExit), self.assertLogs('django.command', 'ERROR') as logs:
            call_command('makemigrations', 'django.contrib.auth')
        self.assertLogRecords(logs, [('ERROR', self.did_you_mean_auth_error, ())])

    def test_migrate_nonexistent_app_label(self):
        with self.assertRaisesMessage(CommandError, self.nonexistent_app_error):
            call_command('migrate', 'nonexistent_app')

    def test_migrate_app_name_specified_as_label(self):
        with self.assertRaisesMessage(CommandError, self.did_you_mean_auth_error):
            call_command('migrate', 'django.contrib.auth')

    def test_showmigrations_nonexistent_app_label(self):
        with self.assertRaises(SystemExit), self.assertLogs('django.command', 'ERROR') as logs:
            call_command('showmigrations', 'nonexistent_app')
        self.assertLogRecords(logs, [('ERROR', self.nonexistent_app_error, ())])

    def test_showmigrations_app_name_specified_as_label(self):
        with self.assertRaises(SystemExit), self.assertLogs('django.command', 'ERROR') as logs:
            call_command('showmigrations', 'django.contrib.auth')
        self.assertLogRecords(logs, [('ERROR', self.did_you_mean_auth_error, ())])

    def test_sqlmigrate_nonexistent_app_label(self):
        with self.assertRaisesMessage(CommandError, self.nonexistent_app_error):
            call_command('sqlmigrate', 'nonexistent_app', '0002')

    def test_sqlmigrate_app_name_specified_as_label(self):
        with self.assertRaisesMessage(CommandError, self.did_you_mean_auth_error):
            call_command('sqlmigrate', 'django.contrib.auth', '0002')

    def test_squashmigrations_nonexistent_app_label(self):
        with self.assertRaisesMessage(CommandError, self.nonexistent_app_error):
            call_command('squashmigrations', 'nonexistent_app', '0002')

    def test_squashmigrations_app_name_specified_as_label(self):
        with self.assertRaisesMessage(CommandError, self.did_you_mean_auth_error):
            call_command('squashmigrations', 'django.contrib.auth', '0002')
