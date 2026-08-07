#!/usr/bin/env python3
"""Capture or verify the production embedding alias against fixed source-controlled probes."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

# Make direct execution from ``scripts/`` resolve repository packages without requiring
# an operator-specific PYTHONPATH.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from db.models import EMBEDDING_DIM  # noqa: E402
from packages.config.settings import get_settings  # noqa: E402
from packages.providers.openai_embeddings import OpenAIEmbeddingProvider  # noqa: E402
from services.nlp.snapshot_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    EmbeddingSnapshotError,
    activate_snapshot,
    capture_snapshot,
    verify_snapshot,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture text-embedding-3-small probe vectors into a verifiable local snapshot, "
            "or replay an existing snapshot to check provider-alias drift."
        )
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="snapshot registry path (default: source-controlled project registry)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("capture")
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot_id")
    activate = subparsers.add_parser("activate")
    activate.add_argument("snapshot_id")
    return parser


def _provider(version: str) -> OpenAIEmbeddingProvider:
    settings = get_settings()
    if not settings.openai_api_key:
        raise EmbeddingSnapshotError(
            "OPENAI_API_KEY is not configured; a real provider response is required and "
            "a snapshot id must never be fabricated"
        )
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model_name=settings.embedding_model,
        model_version=version,
        dimension=EMBEDDING_DIM,
        timeout=settings.llm_request_timeout_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    provider: OpenAIEmbeddingProvider | None = None
    try:
        if args.command == "capture":
            # The capture label is never persisted. The derived snapshot id is based only on
            # canonical returned vectors and is assigned after the response exists.
            provider = _provider("snapshot-capture-pending")
            record = capture_snapshot(
                provider,
                captured_at=datetime.datetime.now(datetime.UTC),
                registry_path=args.registry,
            )
            output = {
                "status": "captured",
                "snapshot_id": record.snapshot_id,
                "manifest_sha256": record.manifest_sha256,
                "response_sha256": record.response_sha256,
                "active": record.status == "active",
                "next_step": (
                    None
                    if record.status == "active"
                    else f"verify and then activate {record.snapshot_id} before configuring it"
                ),
            }
        elif args.command == "verify":
            provider = _provider(args.snapshot_id)
            verified = verify_snapshot(
                provider,
                args.snapshot_id,
                registry_path=args.registry,
            )
            output = {"status": "verified", **verified.as_dict()}
        else:
            provider = _provider(args.snapshot_id)
            verified = activate_snapshot(
                provider,
                args.snapshot_id,
                registry_path=args.registry,
            )
            output = {
                "status": "activated",
                **verified.as_dict(),
                "next_configuration": f"EMBEDDING_MODEL_VERSION={args.snapshot_id}",
            }
    except EmbeddingSnapshotError as exc:
        print(f"embedding snapshot error: {exc}", file=sys.stderr)
        return 2
    finally:
        if provider is not None:
            provider.close()
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
