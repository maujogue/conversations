"""Arena prices come from the LLM configuration, not from the admin (router spec 12).

``LLModel.input_price_eur_per_mtok`` / ``output_price_eur_per_mtok`` are now the only
place a price is declared; ``chat.arena.candidate_prices`` reads them and freezes them
in ``ArenaComparison.price_snapshot``, which is unchanged, so the recorded cost of past
inference is untouched by this removal.
"""

from django.db import migrations


class Migration(migrations.Migration):
    """Remove the manual price fields of the experiment and its challengers."""

    dependencies = [
        ("chat", "0020_arena_per_tier"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="arenachallenger",
            name="input_price_eur_per_mtok",
        ),
        migrations.RemoveField(
            model_name="arenachallenger",
            name="output_price_eur_per_mtok",
        ),
        migrations.RemoveField(
            model_name="arenaexperiment",
            name="champion_input_price_eur_per_mtok",
        ),
        migrations.RemoveField(
            model_name="arenaexperiment",
            name="champion_output_price_eur_per_mtok",
        ),
    ]
