from __future__ import annotations

import argparse
import logging

from .config import Config, default_paths
from .engine import Guardian
from .manifest import Manifest


def main() -> None:
    config_path, manifest_path, log_path = default_paths()
    parser = argparse.ArgumentParser(description="Safeguard camera footage")
    parser.add_argument("--once", action="store_true", help="run one scan without the window")
    parser.add_argument("--config", type=str, default=str(config_path))
    args = parser.parse_args()
    config_path = type(config_path)(args.config).expanduser()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.once:
        from .ui import App
        App(config_path, manifest_path, log_path).mainloop()
        return
    logging.basicConfig(filename=log_path, level=logging.INFO)
    Guardian(Config.load(config_path), Manifest(manifest_path), logging.getLogger("footage_guardian")).scan_once()


if __name__ == "__main__":
    main()
