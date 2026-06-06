import asyncio
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the algo trader (delta-interpolation algorithm from algorithms_2026-05-28.py)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--assets",
            type=str,
            default=None,
            help="Comma-separated assets, e.g. BTC,ETH. Defaults to TradingConfig.assets.",
        )

    def handle(self, *args, **options):
        from trading.models import TradingConfig
        from trading.algo_trader import algo_trader_loop
        from trading.algo_signals import algo_refresh_loop
        from trading.deribit_ws import deribit_ws_loop

        cfg = TradingConfig.load()
        assets_str = options.get("assets") or cfg.assets
        assets = [a.strip().upper() for a in assets_str.split(",") if a.strip()]

        self.stdout.write(f"Starting algo-trader for assets: {assets}")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

        async def _run():
            tasks = [asyncio.create_task(algo_trader_loop(a)) for a in assets]
            tasks.append(asyncio.create_task(algo_refresh_loop()))
            tasks.append(asyncio.create_task(deribit_ws_loop()))
            await asyncio.gather(*tasks)

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            self.stdout.write("Algo-trader stopped.")
