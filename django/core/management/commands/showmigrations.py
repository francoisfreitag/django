import itertools
import sys

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.loader import MigrationLoader


class Command(BaseCommand):
    help = "Shows all available migrations for the current project"

    def add_arguments(self, parser):
        parser.add_argument(
            'app_label', nargs='*',
            help='App labels of applications to limit the output to.',
        )
        parser.add_argument(
            '--database', default=DEFAULT_DB_ALIAS,
            help='Nominates a database to synchronize. Defaults to the "default" database.',
        )

        formats = parser.add_mutually_exclusive_group()
        formats.add_argument(
            '--list', '-l', action='store_const', dest='format', const='list',
            help=(
                'Shows a list of all migrations and which are applied. '
                'With a verbosity level of 2 or above, the applied datetimes '
                'will be included.'
            ),
        )
        formats.add_argument(
            '--plan', '-p', action='store_const', dest='format', const='plan',
            help=(
                'Shows all migrations in the order they will be applied. '
                'With a verbosity level of 2 or above all direct migration dependencies '
                'and reverse dependencies (run_before) will be included.'
            )
        )

        parser.set_defaults(format='list')

    def handle(self, *args, **options):
        self.verbosity = options['verbosity']

        # Get the database we're operating from
        db = options['database']
        connection = connections[db]

        if options['format'] == "plan":
            return self.show_plan(connection, options['app_label'])
        else:
            return self.show_list(connection, options['app_label'])

    def _validate_app_names(self, loader, app_names):
        has_bad_names = False
        for app_name in app_names:
            try:
                apps.get_app_config(app_name)
            except LookupError as err:
                self.logger.error(str(err))
                has_bad_names = True
        if has_bad_names:
            sys.exit(2)

    def show_list(self, connection, app_names=None):
        """
        Show a list of all migrations on the system, or only those of
        some named apps.
        """
        # Load migrations from disk/DB
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        graph = loader.graph
        # If we were passed a list of apps, validate it
        if app_names:
            self._validate_app_names(loader, app_names)
        # Otherwise, show all apps in alphabetic order
        else:
            app_names = sorted(loader.migrated_apps)
        # For each app, print its migrations in order from oldest (roots) to
        # newest (leaves).
        for app_name in app_names:
            self.logger.info(self.style.MIGRATE_LABEL("%s"), app_name)
            shown = set()
            for node in graph.leaf_nodes(app_name):
                for plan_node in graph.forwards_plan(node):
                    if plan_node not in shown and plan_node[0] == app_name:
                        # Give it a nice title if it's a squashed one
                        title = plan_node[1]
                        if graph.nodes[plan_node].replaces:
                            title += " (%s squashed migrations)" % len(graph.nodes[plan_node].replaces)
                        applied_migration = loader.applied_migrations.get(plan_node)
                        # Mark it as applied/unapplied
                        if applied_migration:
                            args = [title]
                            output = ' [X] %s'
                            if self.verbosity >= 2:
                                args.append(applied_migration.applied.strftime('%Y-%m-%d %H:%M:%S'))
                                output += ' (applied at %s)'
                            self.logger.info(output, *args)
                        else:
                            self.logger.info(" [ ] %s", title)
                        shown.add(plan_node)
            # If we didn't print anything, then a small message
            if not shown:
                self.logger.info(self.style.ERROR(" (no migrations)"))

    def show_plan(self, connection, app_names=None):
        """
        Show all known migrations (or only those of the specified app_names)
        in the order they will be applied.
        """
        # Load migrations from disk/DB
        loader = MigrationLoader(connection)
        graph = loader.graph
        if app_names:
            self._validate_app_names(loader, app_names)
            targets = [key for key in graph.leaf_nodes() if key[0] in app_names]
        else:
            targets = graph.leaf_nodes()
        plan = []
        seen = set()

        # Generate the plan
        for target in targets:
            for migration in graph.forwards_plan(target):
                if migration not in seen:
                    node = graph.node_map[migration]
                    plan.append(node)
                    seen.add(migration)

        # Output
        def format_deps(node):
            # parent.key is a tuple (app_label, migration_name).
            parent_keys = [parent.key for parent in sorted(node.parents)]
            if parent_keys:
                args = itertools.chain.from_iterable(parent_keys)
                placeholders = ", ".join(["%s.%s"] * len(parent_keys))
                return " ... (" + placeholders + ")", args
            return "", []

        for node in plan:
            deps_placeholder, deps_args = "", []
            if self.verbosity >= 2:
                deps_placeholder, deps_args = format_deps(node)
            if node.key in loader.applied_migrations:
                self.logger.info(
                    "[X]  %s.%s{0}".format(deps_placeholder),
                    node.key[0], node.key[1], *deps_args)
            else:
                self.logger.info(
                    "[ ]  %s.%s{0}".format(deps_placeholder),
                    node.key[0], node.key[1], *deps_args)
        if not plan:
            self.logger.info(self.style.ERROR('(no migrations)'))
