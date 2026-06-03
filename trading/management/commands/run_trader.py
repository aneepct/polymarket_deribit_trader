import asyncio
import logging

from django.core.management.base import BaseCommand


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the autonomous trading loop for all configured assets."

    def add_arguments(self, parser):
        parser.add_argument(
            "--assets",
            type=str,
            default=None,
            help="Comma-separated assets to trade, e.g. BTC,ETH. Defaults to TradingConfig.assets.",
        )

    def handle(self, *args, **options):
        from trading.models import TradingConfig
        from trading.auto_trader import auto_trader_loop

        cfg = TradingConfig.load()
        assets_str = options.get("assets") or cfg.assets
        assets = [a.strip().upper() for a in assets_str.split(",") if a.strip()]

        self.stdout.write(f"Starting auto-trader for assets: {assets}")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

        async def _run():
            from trading.csv_refresh import csv_refresh_loop
            refresh_interval = getattr(cfg, "scan_interval_s", 3600)
            tasks = [asyncio.create_task(auto_trader_loop(a)) for a in assets]
            tasks.append(asyncio.create_task(csv_refresh_loop(interval_seconds=refresh_interval)))
            await asyncio.gather(*tasks)

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            self.stdout.write("Trader stopped.")
