from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0032_expand_allotted_time_precision"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="quality_of_task",
            field=models.CharField(
                blank=True,
                choices=[
                    ("excellent", "Excellent"),
                    ("good", "Good"),
                    ("needs_improvement", "Satisfactory"),
                    ("rework_needed", "Below Expectations"),
                    ("rejected", "Unsatisfactory"),
                ],
                max_length=25,
            ),
        ),
    ]
