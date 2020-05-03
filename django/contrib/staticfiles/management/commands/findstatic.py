import os

from django.contrib.staticfiles import finders
from django.core.management.base import LabelCommand


class Command(LabelCommand):
    help = "Finds the absolute paths for the given static file(s)."
    label = 'staticfile'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            '--first', action='store_false', dest='all',
            help="Only return the first match for each static file.",
        )

    def handle_label(self, path, **options):
        verbosity = options['verbosity']
        result = finders.find(path, all=options['all'])
        if verbosity >= 2:
            locations = finders.searched_locations
            searched_locations = '\nLooking in the following locations:\n  {0}'.format(
                '\n  '.join(['%s'] * len(locations)))
            searched_locations_args = [str(loc) for loc in locations]
        else:
            searched_locations = ''
            searched_locations_args = []
        if result:
            if not isinstance(result, (list, tuple)):
                result = [result]
            result = [os.path.realpath(path) for path in result]
            if verbosity >= 1:
                file_list = '\n  '.join(['%s'] * len(result))
                return ("Found '%s' here:\n  {}{}".format(file_list, searched_locations),
                        path, *result, *searched_locations_args)
            else:
                return ('\n'.join(['%s'] * len(result)), *result)
        else:
            message = "No matching file found for '%s'."
            args = [path]
            if verbosity >= 2:
                message += "\n" + searched_locations
                args.extend(searched_locations_args)
            if verbosity >= 1:
                self.logger.error(message, *args)
