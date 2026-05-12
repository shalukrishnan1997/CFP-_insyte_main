import os

import django


def main() -> None:
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "responsehandling.settings.development"
    )
    django.setup()

    from django.apps import apps
    from django.db.models.fields.related import (
        ForeignKey,
        ManyToManyField,
        OneToOneField,
    )

    for model in apps.get_models():
        app_label = model._meta.app_label
        if app_label in {
            "admin",
            "auth",
            "contenttypes",
            "sessions",
            "messages",
            "staticfiles",
            "authtoken",
            "corsheaders",
        }:
            continue

        print(f"Model: {model._meta.label}")
        print(f"Table: {model._meta.db_table}")
        for field in model._meta.get_fields():
            if field.is_relation and field.related_model:
                if isinstance(field, ForeignKey):
                    rel_type = "ForeignKey"
                elif isinstance(field, ManyToManyField):
                    rel_type = "ManyToMany"
                elif isinstance(field, OneToOneField):
                    rel_type = "OneToOne"
                else:
                    rel_type = field.__class__.__name__
                print(
                    f"  - {field.name}: {rel_type} to {field.related_model._meta.label}"
                )
            else:
                try:
                    print(f"  - {field.name}: {field.get_internal_type()}")
                except AttributeError:
                    print(f"  - {field.name}: {field.__class__.__name__}")
        print()


if __name__ == "__main__":
    main()
