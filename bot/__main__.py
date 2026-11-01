from __future__ import annotations

import logging

from .app import create_bot
from .config import Config, ConfigError


def main() -> None:
    # Keep dependency request logs (which can contain URLs) out of output. The bot's audit logger emits only
    # command name, a salted guild hash and status.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("banthefake.discord").setLevel(logging.INFO)
    try:
        config = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    create_bot(config).run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
