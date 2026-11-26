from django.db import migrations, models


def backfill_recurring_departments(apps, schema_editor):
    RecurringTaskDefinition = apps.get_model("tasks", "RecurringTaskDefinition")
    Task = apps.get_model("tasks", "Task")
    database = schema_editor.connection.alias

    for definition in RecurringTaskDefinition.objects.using(database).all().iterator():
        department = (
            Task.objects.using(database)
            .filter(recurring_source_id=definition.pk)
            .exclude(department="")
            .values_list("department", flat=True)
            .first()
        )
        if not department and definition.assigned_to_id:
            Employee = apps.get_model("accounts", "Employee")
            department = (
                Employee.objects.using(database)
                .filter(pk=definition.assigned_to_id)
                .values_list("department", flat=True)
                .first()
            )
        definition.department = department or ""
        definition.save(update_fields=["department"], using=database)


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0034_task_creator_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringtaskdefinition",
            name="department",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.RunPython(backfill_recurring_departments, migrations.RunPython.noop),
    ]
