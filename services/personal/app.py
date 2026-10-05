"""Start one loopback application supervisor; the application also serves the personal browser.

Usage: python -m services.personal.app --port 8000
"""

from __future__ import annotations

import argparse


def main() -> None:
    from packages.config.settings import get_settings
    from services.personal.supervisor import validate_personal_runtime_settings

    parser = argparse.ArgumentParser(description="Start the local personal news-desk app")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    settings = get_settings()
    validate_personal_runtime_settings(settings)
    import uvicorn

    uvicorn.run("apps.api.main:app", host=settings.personal_bind_host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
