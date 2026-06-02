from django.contrib import admin
from .models import TradingConfig, PolymarketCredentials


@admin.register(TradingConfig)
class TradingConfigAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Order Sizing", {
            "fields": ("order_usd", "min_shares"),
        }),
        ("Profit / Loss", {
            "fields": ("profit_target_pct", "stop_loss_pct"),
        }),
        ("Timing", {
            "fields": ("scan_interval_s", "today_lookahead_hours"),
        }),
        ("Deribit Signal", {
            "fields": ("deribit_neutral_low", "deribit_neutral_high"),
        }),
        ("Assets", {
            "fields": ("assets",),
        }),
        ("CLOB / Chain", {
            "fields": ("chain_id",),
        }),
        ("Positions API", {
            "fields": ("positions_limit", "positions_size_threshold"),
        }),
        ("HTTP & Redis", {
            "fields": ("http_timeout_s", "redis_state_ttl_hours"),
        }),
        ("API Endpoints", {
            "fields": ("polymarket_clob_api", "polymarket_data_api", "gamma_api"),
            "classes": ("collapse",),
        }),
    )

    def has_add_permission(self, request):
        return not TradingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PolymarketCredentials)
class PolymarketCredentialsAdmin(admin.ModelAdmin):
    fields = ("private_key", "funder_address")

    def has_add_permission(self, request):
        return not PolymarketCredentials.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
