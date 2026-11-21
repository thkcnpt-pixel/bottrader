"""Send FCM alert & record failure heartbeat when a GitHub Actions workflow fails."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import LOG_LEVEL  # noqa: E402
from utils.firebase_client import FirebaseClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("notify_workflow_failure")


def main() -> None:
    parser = argparse.ArgumentParser(description="Notify owner on GitHub Actions workflow failure.")
    parser.add_argument("--workflow", required=True, help="Workflow identifier (e.g. daily_scan)")
    parser.add_argument("--run-url", default="", help="GitHub Actions run URL")
    args = parser.parse_args()

    try:
        fb = FirebaseClient()
        fb.record_heartbeat(
            args.workflow,
            status="failed",
            error_message=f"GitHub Actions workflow failed ({args.run_url})".strip(),
        )
        body = f"Workflow '{args.workflow}' trên GitHub Actions bị lỗi."
        if args.run_url:
            body += f" Kiểm tra log tại: {args.run_url}"
        fb.send_system_alert(
            workflow=args.workflow,
            title=f"🚨 GitHub Actions lỗi: {args.workflow}",
            body=body,
            dedup_key="gh_workflow_failure",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not send workflow failure notification: %s", exc)


if __name__ == "__main__":
    main()
