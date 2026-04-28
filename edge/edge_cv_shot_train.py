"""Train the shot-centric crosshair crop classifier on labeled shot crops."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from edge.edge_cv_train import (
    download_manifest,
    report_progress,
    resolve_local_crops,
    train_classifier,
    upload_model,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train per-node shot classifier")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--node-slug", required=True)
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--job-id", required=True, type=int)
    parser.add_argument("--manifest-key", required=True)
    parser.add_argument("--model-version", required=True, type=int)
    parser.add_argument("--crops-dir", required=True, help="Local directory with shot crop images")
    parser.add_argument("--base-model", default=None, help="Existing shot classifier model to fine-tune from")
    parser.add_argument("--work-dir", default="/tmp/cv-shot-training")
    parser.add_argument("--no-ssl-verify", action="store_true")
    args = parser.parse_args()

    ssl_verify = not args.no_ssl_verify
    work_dir = Path(args.work_dir) / f"job-{args.job_id}"
    data_dir = work_dir / "data"
    model_path = work_dir / "model.pth"
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        report_progress(args.base_url, args.node_slug, args.auth_token, args.job_id, "preparing", ssl_verify=ssl_verify)

        print("Downloading shot manifest...")
        manifest = download_manifest(args.base_url, args.node_slug, args.manifest_key, args.auth_token, ssl_verify)
        print(f"Manifest: {len(manifest['samples'])} samples, {len(manifest['classes'])} classes")

        print("Resolving local shot crops...")
        found = resolve_local_crops(
            args.crops_dir, manifest["samples"], data_dir,
            base_url=args.base_url, node_slug=args.node_slug,
            auth_token=args.auth_token, ssl_verify=ssl_verify,
        )
        if found == 0:
            raise ValueError("No local shot crop images found")

        print("Training shot classifier...")
        metrics = train_classifier(
            data_dir, manifest["classes"], model_path,
            args.base_url, args.node_slug, args.auth_token, args.job_id, ssl_verify,
            base_model_path=args.base_model,
        )
        metrics["training_type"] = "shot"
        print(f"Training complete: {metrics['accuracy']:.1%} accuracy")

        model_key = f"models/{args.node_slug}/shot-classifier-latest.pth"
        print("Uploading shot model...")
        upload_model(args.base_url, args.node_slug, args.auth_token, model_path, model_key, ssl_verify)

        report_progress(
            args.base_url, args.node_slug, args.auth_token, args.job_id,
            "completed", metrics=metrics, model_key=model_key, ssl_verify=ssl_verify,
        )
        print("Done!")
    except Exception as exc:
        print(f"Shot training failed: {exc}")
        report_progress(
            args.base_url, args.node_slug, args.auth_token, args.job_id,
            "failed", error=str(exc)[:500], ssl_verify=ssl_verify,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
