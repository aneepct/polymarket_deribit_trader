from django.db import migrations, models
import trading.encryption


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TradingConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_usd", models.FloatField(default=5.0, help_text="Target USDC to spend per trade")),
                ("min_shares", models.FloatField(default=5.0, help_text="Polymarket CLOB minimum order size (shares)")),
                ("profit_target_pct", models.FloatField(default=5.0, help_text="Close position when P&L % reaches this value")),
                ("stop_loss_pct", models.FloatField(default=-20.0, help_text="Close position when P&L % drops to this value (negative)")),
                ("scan_interval_s", models.IntegerField(default=60, help_text="Seconds between scan / monitor cycles")),
                ("deribit_neutral_low", models.FloatField(default=0.49)),
                ("deribit_neutral_high", models.FloatField(default=0.51)),
                ("today_lookahead_hours", models.FloatField(default=6.0)),
                ("polymarket_clob_api", models.CharField(default="https://clob.polymarket.com", max_length=255)),
                ("polymarket_data_api", models.CharField(default="https://data-api.polymarket.com", max_length=255)),
                ("gamma_api", models.CharField(default="https://gamma-api.polymarket.com", max_length=255)),
                ("assets", models.CharField(default="BTC,ETH", max_length=100)),
                ("chain_id", models.IntegerField(default=137, help_text="EVM chain ID (137 = Polygon mainnet)")),
                ("positions_limit", models.IntegerField(default=100, help_text="Max number of positions to fetch per API call")),
                ("positions_size_threshold", models.FloatField(default=0.01, help_text="Minimum position size to include in fetch")),
                ("http_timeout_s", models.FloatField(default=10.0, help_text="Timeout in seconds for all outbound HTTP requests")),
                ("redis_state_ttl_hours", models.FloatField(default=48.0, help_text="Hours before Redis trader-state keys expire")),
            ],
            options={"verbose_name": "Trading Configuration", "verbose_name_plural": "Trading Configuration"},
        ),
        migrations.CreateModel(
            name="PolymarketCredentials",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("private_key", trading.encryption.EncryptedCharField(blank=True, default="")),
                ("funder_address", models.CharField(blank=True, default="", max_length=255)),
            ],
            options={"verbose_name": "Polymarket Credentials", "verbose_name_plural": "Polymarket Credentials"},
        ),
    ]
