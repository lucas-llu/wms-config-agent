"""WMS Config Agent application entry point."""

from core.settings import load_settings
from observability.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    """Load and validate configuration before starting the application."""
    settings = load_settings()
    logger.info(
        "Configuration loaded for project=%s environment=%s",
        settings.project.name,
        settings.project.environment,
    )
    print(f"{settings.project.name} is ready ({settings.project.environment}).")


if __name__ == "__main__":
    main()
