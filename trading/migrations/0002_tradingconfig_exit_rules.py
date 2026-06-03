from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0001_initial"),
    ]

    operations = [
        # Rename default for stop_loss_pct (was -20.0, now -50.0 for extras fallback)
        migrations.AlterField(
            model_name="tradingconfig",
            name="stop_loss_pct",
            field=models.FloatField(
                default=-50.0,
                help_text=(
                    "Close extra positions when P&L % drops to this value (negative). "
                    "Primary position uses signal-based exit."
                ),
            ),
        ),
        # New exit-rule fields
        migrations.AddField(
            model_name="tradingconfig",
            name="min_fair_prob",
            field=models.FloatField(
                default=0.51,
                help_text=(
                    "Exit primary position if Deribit fair probability for the held side "
                    "drops below this value (e.g. 0.51 = 51%)"
                ),
            ),
        ),
        migrations.AddField(
            model_name="tradingconfig",
            name="early_collapse_window_s",
            field=models.IntegerField(
                default=600,
                help_text=(
                    "Seconds after fill during which the early-collapse rule is active "
                    "(default 600 = 10 min)"
                ),
            ),
        ),
        migrations.AddField(
            model_name="tradingconfig",
            name="early_collapse_edge_threshold",
            field=models.FloatField(
                default=0.01,
                help_text=(
                    "Exit if |edge| drops below this fraction within the early-collapse "
                    "window (e.g. 0.01 = 1pp)"
                ),
            ),
        ),
    ]
