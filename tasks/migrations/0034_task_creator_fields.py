from django.db import migrations, models
import django.db.models.deletion


def populate_task_creators(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    ActivityLog = apps.get_model("tasks", "ActivityLog")
    database = schema_editor.connection.alias

    for task in Task.objects.using(database).all().iterator():
        creation = (
            ActivityLog.objects.using(database)
            .filter(task_id=task.pk, action="created")
            .order_by("created_at")
            .first()
        )
        if creation and (creation.actor_admin_id or creation.actor_employee_id):
            task.created_by_admin_id = creation.actor_admin_id
            task.created_by_employee_id = creation.actor_employee_id
        else:
            # Older quick-created tasks have no creation activity entry, so use the
            # historical reporter as the best available creator record.
            task.created_by_admin_id = task.assigned_by_admin_id
            task.created_by_employee_id = task.assigned_by_employee_id
        task.save(update_fields=["created_by_admin", "created_by_employee"], using=database)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_employee_belong_to_admin"),
        ("tasks", "0033_update_task_quality_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="created_by_admin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks_authored",
                to="accounts.admin",
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="created_by_employee",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks_authored_as_employee",
                to="accounts.employee",
            ),
        ),
        migrations.RunPython(populate_task_creators, migrations.RunPython.noop),
    ]
