"""
Django models for the trading app.

TradingConfig  — singleton row holding all tuning parameters; editable in admin.
PolymarketCredentials — encrypted private key + funder address; editable in admin.
"""
from django.db import models
from django.core.exceptions import ValidationError

from .encryption import EncryptedCharField


# ---------------------------------------------------------------------------
# Singleton mixin
# ---------------------------------------------------------------------------

class SingletonModel(models.Model):
    """Ensures only one row can ever exist in the table."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # prevent deletion

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ---------------------------------------------------------------------------
# Trading tuning parameters  (all previously hard-coded constants / env vars)
# ---------------------------------------------------------------------------

class TradingConfig(SingletonModel):
    """
    All tuning constants for the auto-trader.
    Edit via Django admin — changes take effect on the next cycle (no restart).
    Set trading_enabled=False to pause the loop without stopping the process.
    """

    # ── Scheduler control ───────────────────────────────────────────────────
    trading_enabled = models.BooleanField(
        default=True,
        help_text="Uncheck to pause the trading loop. Re-check to resume. Takes effect within one cycle.",
    )

    # ── Auto-trader constants ────────────────────────────────────────────────
    order_usd = models.FloatField(
        default=5.0,
        help_text="Target USDC to spend per trade",
    )
    min_shares = models.FloatField(
        default=5.0,
        help_text="Polymarket CLOB minimum order size (shares)",
    )
    profit_target_pct = models.FloatField(
        default=5.0,
        help_text="Close position when P&L % reaches this value",
    )
    stop_loss_pct = models.FloatField(
        default=-50.0,
        help_text="Close extra positions when P&L % drops to this value (negative). Primary position uses signal-based exit.",
    )
    scan_interval_s = models.IntegerField(
        default=60,
        help_text="Seconds between scan / monitor cycles",
    )
    deribit_neutral_low = models.FloatField(
        default=0.49,
        help_text="Lower bound of Deribit neutral band (skip if prob is in this range)",
    )
    deribit_neutral_high = models.FloatField(
        default=0.51,
        help_text="Upper bound of Deribit neutral band",
    )
    today_lookahead_hours = models.FloatField(
        default=6.0,
        help_text="Hours added to now() when checking if a market resolves 'today'",
    )

    # ── Signal-based exit rules (spec v1.1 + early-collapse 2026-05-25) ─────
    min_fair_prob = models.FloatField(
        default=0.51,
        help_text="Exit primary position if Deribit fair probability for the held side drops below this value (e.g. 0.51 = 51%)",
    )
    early_collapse_window_s = models.IntegerField(
        default=600,
        help_text="Seconds after fill during which the early-collapse rule is active (default 600 = 10 min)",
    )
    early_collapse_edge_threshold = models.FloatField(
        default=0.01,
        help_text="Exit if |edge| drops below this fraction within the early-collapse window (e.g. 0.01 = 1pp)",
    )

    # ── API endpoints ────────────────────────────────────────────────────────
    polymarket_clob_api = models.CharField(
        max_length=255,
        default="https://clob.polymarket.com",
    )
    polymarket_data_api = models.CharField(
        max_length=255,
        default="https://data-api.polymarket.com",
    )
    gamma_api = models.CharField(
        max_length=255,
        default="https://gamma-api.polymarket.com",
    )

    # ── CLOB / chain ────────────────────────────────────────────────────────
    chain_id = models.IntegerField(
        default=137,
        help_text="EVM chain ID (137 = Polygon mainnet)",
    )

    # ── Positions query ──────────────────────────────────────────────────────
    positions_limit = models.IntegerField(
        default=100,
        help_text="Max number of positions to fetch per API call",
    )
    positions_size_threshold = models.FloatField(
        default=0.01,
        help_text="Minimum position size to include in fetch (sizeThreshold)",
    )

    # ── HTTP ─────────────────────────────────────────────────────────────────
    http_timeout_s = models.FloatField(
        default=10.0,
        help_text="Timeout in seconds for all outbound HTTP requests",
    )

    # ── Redis state TTL ──────────────────────────────────────────────────────
    redis_state_ttl_hours = models.FloatField(
        default=48.0,
        help_text="Hours before Redis trader-state keys expire (safety TTL)",
    )

    # ── Assets ───────────────────────────────────────────────────────────────
    assets = models.CharField(
        max_length=100,
        default="BTC,ETH",
        help_text="Comma-separated list of assets to trade, e.g. BTC,ETH",
    )

    class Meta:
        verbose_name = "Trading Configuration"
        verbose_name_plural = "Trading Configuration"

    def __str__(self):
        return "Trading Configuration"

    def asset_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    def clean(self):
        if self.stop_loss_pct >= 0:
            raise ValidationError({"stop_loss_pct": "Stop-loss must be negative."})
        if self.profit_target_pct <= 0:
            raise ValidationError({"profit_target_pct": "Profit target must be positive."})


# ---------------------------------------------------------------------------
# Encrypted Polymarket credentials
# ---------------------------------------------------------------------------

class PolymarketCredentials(SingletonModel):
    """
    Stores Polymarket private key and funder address encrypted at rest.
    The encryption key lives only in .env (FIELD_ENCRYPTION_KEY).
    """

    private_key = EncryptedCharField(
        blank=True,
        default="",
        help_text="Polymarket EVM private key — stored encrypted",
    )
    funder_address = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Funder wallet address (0x…)",
    )

    class Meta:
        verbose_name = "Polymarket Credentials"
        verbose_name_plural = "Polymarket Credentials"

    def __str__(self):
        return "Polymarket Credentials"
