"""Entrypoint for OpenAdapt."""

import multiprocessing

if __name__ == "__main__":
    # This needs to be called before any code that uses multiprocessing
    multiprocessing.freeze_support()

from legacy.openadapt.build_utils import redirect_stdout_stderr
from legacy.openadapt.error_reporting import configure_error_reporting
from legacy.openadapt.custom_logger import logger


def run_openadapt() -> None:
    """Run OpenAdapt."""
    with redirect_stdout_stderr():
        try:
            from legacy.openadapt.alembic.context_loader import load_alembic_context
            from legacy.openadapt.app import tray
            from legacy.openadapt.config import print_config

            print_config()
            configure_error_reporting()
            load_alembic_context()
            tray._run()
        except Exception as exc:
            logger.exception(exc)


if __name__ == "__main__":
    run_openadapt()
