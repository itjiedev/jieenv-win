import os
from pathlib import Path
from django.conf import settings
from django.core.management.templates import TemplateCommand
from django.core.management.base import CommandError

class Command(TemplateCommand):

    rewrite_template_suffixes = (
        (".py-tpl", ".py"),
    )

    def handle(self, *args, **options):
        app_name = options.pop('name')
        options["template"] = str(Path(__file__).resolve().parent / "app_template")

        if options['directory']:
            target = options.pop('directory')
        else:
            target = os.path.join(settings.BASE_DIR, 'apps', app_name)
            try:
                os.makedirs(target)
            except FileExistsError:
                raise CommandError("The app folder %s  already exists. Please choose another app name。" % target)
            except OSError as e:
                raise CommandError(e)
        options['target'] = target
        super().handle("app", app_name, **options)
