from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0031_normaltask_recurringtask_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="allotted_time",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Allotted time in hours",
                max_digits=8,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="recurringtaskdefinition",
            name="allotted_time",
            field=models.DecimalField(max_digits=8, decimal_places=2),
        ),
    ]
