#!/usr/bin/env python3
import asyncio
import logging
from TelegramReminder import main as reminder_main  # assumes your script exposes a `main()` coroutine

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

async def _run():
    try:
        await reminder_main()
    except asyncio.CancelledError:
        logger.info("Shutdown requested, exiting.")
    except Exception:
        logger.exception("Unhandled exception in reminder loop")

if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user, exiting.")
    except Exception:
        logger.exception("Fatal error in autorun wrapper")
