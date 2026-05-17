from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler


stderr_console = Console(stderr=True)
stdout_console = Console()


def configure_logging(
    *,
    verbose: int = 0,
    quiet: bool = False,
    log_file: str | None = None,
) -> None:
    if quiet:
        level = logging.WARNING
    elif verbose > 0:
        level = logging.DEBUG
    else:
        level = logging.INFO

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    console_handler = RichHandler(
        console=stderr_console,
        rich_tracebacks=True,
        show_path=verbose > 0,
        markup=False,
    )
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        root.addHandler(file_handler)

    logging.captureWarnings(True)
