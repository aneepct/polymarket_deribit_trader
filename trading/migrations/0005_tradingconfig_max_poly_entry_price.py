from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0004_deribit_neutral_band_wider"),
    ]

    operations = [
        migrations.AddField(
            model_name="tradingconfig",
            name="max_poly_entry_price",
            field=models.FloatField(
                default=0.70,
                help_text=(
                    "Maximum token entry price (0–1). Entries above this are skipped regardless of edge. "
                    "At 0.90 entry: max profit is 10¢ but max loss is 90¢ — terrible risk/reward. "
                    "Default 0.70 blocks entries where BSM model error risk outweighs reward."
                ),
            ),
        ),
    ]
