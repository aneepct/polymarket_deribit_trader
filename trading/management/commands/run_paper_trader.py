import asyncio
import json
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the paper trader (simulated trades, no real orders placed)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--assets",
            type=str,
            default=None,
            help="Comma-separated assets, e.g. BTC,ETH. Defaults to TradingConfig.assets.",
        )
        parser.add_argument(
            "--summary",
            action="store_true",
            help="Print the current paper P&L summary and exit.",
        )
        parser.add_argument(
            "--log",
            action="store_true",
            help="Print all completed paper trades from the log file and exit.",
        )

    def handle(self, *args, **options):
        from trading.models import TradingConfig
        from trading.paper_trader import paper_trader_loop, PaperPnL, _log_path

        cfg = TradingConfig.load()

        # ── --summary: print running totals and exit ───────────────────────────
        if options.get("summary"):
            for asset in cfg.asset_list():
                s = PaperPnL(asset).summary()
                self.stdout.write(
                    f"{asset}: {s['total_trades']} trades | "
                    f"{s['wins']}W {s['losses']}L | "
                    f"net ${s['total_pnl_usd']:.4f} | "
                    f"capital deployed ${s['total_spent_usd']:.2f}"
                )
            return

        # ── --log: print completed trade log and exit ──────────────────────────
        if options.get("log"):
            path = _log_path()
            if not path.exists():
                self.stdout.write("No paper trades logged yet.")
                return
            with path.open(encoding="utf-8") as f:
                lines = f.readlines()
            self.stdout.write(f"{len(lines)} paper trades in {path}\n")
            total_net = 0.0
            for line in lines:
                try:
                    t = json.loads(line)
                    net = t.get("net_usd", 0.0)
                    total_net += net
                    self.stdout.write(
                        f"  {t.get('asset')} {t.get('outcome')} | "
                        f"'{str(t.get('question', ''))[:55]}' | "
                        f"entry={t.get('entry_price'):.4f} exit={t.get('exit_price'):.4f} | "
                        f"pnl={t.get('pnl_pct'):.1f}% net=${net:.4f} | "
                        f"{t.get('exit_reason')}"
                    )
                except Exception:
                    pass
            self.stdout.write(f"\nTotal net: ${total_net:.4f}")
            return

        # ── Normal: run the paper trading loop ────────────────────────────────
        assets_str = options.get("assets") or cfg.assets
        assets = [a.strip().upper() for a in assets_str.split(",") if a.strip()]

        log_path = _log_path()
        self.stdout.write(f"Starting paper trader for assets: {assets}")
        self.stdout.write(f"Trade log: {log_path}")
        self.stdout.write("No real orders will be placed.")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

        async def _run():
            from trading.algo_signals import algo_refresh_loop
            from trading.deribit_ws import deribit_ws_loop
            tasks = [asyncio.create_task(paper_trader_loop(a)) for a in assets]
            tasks.append(asyncio.create_task(algo_refresh_loop()))
            tasks.append(asyncio.create_task(deribit_ws_loop()))
            await asyncio.gather(*tasks)

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            self.stdout.write("\nPaper trader stopped.")
            # Print final summary on exit
            for asset in assets:
                s = PaperPnL(asset).summary()
                self.stdout.write(
                    f"{asset} final: {s['total_trades']} trades | "
                    f"{s['wins']}W {s['losses']}L | net ${s['total_pnl_usd']:.4f}"
                )
