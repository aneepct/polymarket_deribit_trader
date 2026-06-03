from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0002_tradingconfig_exit_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="tradingconfig",
            name="trading_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Uncheck to pause the trading loop. Re-check to resume. Takes effect within one cycle.",
            ),
        ),
    ]
