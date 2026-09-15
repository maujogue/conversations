# Generated for arena tie / both bad votes

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0016_arena_champion_committed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="arenacomparison",
            name="winner",
            field=models.CharField(
                blank=True,
                choices=[
                    ("champion", "CHAMPION"),
                    ("challenger", "CHALLENGER"),
                    ("tie", "TIE"),
                    ("both_bad", "BOTH_BAD"),
                ],
                default="",
                max_length=10,
            ),
        ),
    ]
