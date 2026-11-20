"""Firebase Firestore & Cloud Messaging (FCM) Admin client."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from utils.config import (
    COL_DAILY_REPORTS,
    COL_FUNDAMENTALS,
    COL_INTRADAY_STATE,
    COL_PORTFOLIO,
    COL_SETTINGS,
    DOC_FCM_TOKENS,
    DRY_RUN,
    vn_now,
)

logger = logging.getLogger(__name__)


class FirebaseClient:
    """Handles Firestore CRUD and FCM push notifications."""

    def __init__(self, dry_run: bool = DRY_RUN) -> None:
        self.dry_run = dry_run
        self._db = None
        self._messaging = None
        if not self.dry_run:
            self._init_firebase()

    def _init_firebase(self) -> None:
        import firebase_admin
        from firebase_admin import credentials, firestore, messaging

        if not firebase_admin._apps:
            sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
            sa_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()

            if sa_json:
                cred_dict = json.loads(sa_json)
                cred = credentials.Certificate(cred_dict)
            elif sa_path and os.path.exists(sa_path):
                cred = credentials.Certificate(sa_path)
            else:
                raise RuntimeError(
                    "Missing Firebase credentials. Set FIREBASE_SERVICE_ACCOUNT_JSON "
                    "or FIREBASE_SERVICE_ACCOUNT_PATH."
                )
            firebase_admin.initialize_app(cred)

        self._db = firestore.client()
        self._messaging = messaging

    # -------------------- Fundamentals --------------------
    def get_fundamental(self, ticker: str) -> Optional[Dict[str, Any]]:
        if self.dry_run:
            return None
        doc = self._db.collection(COL_FUNDAMENTALS).document(ticker).get()
        return doc.to_dict() if doc.exists else None

    def get_all_fundamentals(self) -> Dict[str, Dict[str, Any]]:
        if self.dry_run:
            return {}
        docs = self._db.collection(COL_FUNDAMENTALS).stream()
        return {d.id: d.to_dict() for d in docs}

    def save_fundamental(self, ticker: str, data: Dict[str, Any]) -> None:
        payload = {**data, "updated_at": vn_now().isoformat()}
        if self.dry_run:
            logger.info("[DRY_RUN] save_fundamental(%s): score=%s", ticker, payload.get("score"))
            return
        self._db.collection(COL_FUNDAMENTALS).document(ticker).set(payload, merge=True)

    # -------------------- Portfolio --------------------
    def get_holding_portfolio(self) -> List[Dict[str, Any]]:
        if self.dry_run:
            return []
        docs = (
            self._db.collection(COL_PORTFOLIO)
            .where("status", "==", "holding")
            .stream()
        )
        items = []
        for d in docs:
            item = d.to_dict() or {}
            item.setdefault("ticker", d.id)
            items.append(item)
        return items

    def update_portfolio_fields(self, ticker: str, updates: Dict[str, Any]) -> None:
        if self.dry_run:
            logger.info("[DRY_RUN] update_portfolio(%s): %s", ticker, updates)
            return
        self._db.collection(COL_PORTFOLIO).document(ticker).set(updates, merge=True)

    # -------------------- Daily Reports --------------------
    def save_daily_report(self, date_str: str, report: Dict[str, Any]) -> None:
        payload = {**report, "date": date_str, "updated_at": vn_now().isoformat()}
        if self.dry_run:
            logger.info("[DRY_RUN] save_daily_report(%s): %d candidates, %d portfolio alerts",
                        date_str, len(payload.get("buy_candidates", [])), len(payload.get("portfolio_alerts", [])))
            return
        self._db.collection(COL_DAILY_REPORTS).document(date_str).set(payload)

    # -------------------- Intraday Anti-Spam State --------------------
    def get_sent_intraday_keys(self, date_str: str) -> set:
        if self.dry_run:
            return set()
        doc = self._db.collection(COL_INTRADAY_STATE).document(date_str).get()
        if not doc.exists:
            return set()
        data = doc.to_dict() or {}
        return set(data.get("sent_keys", []))

    def mark_intraday_keys_sent(self, date_str: str, new_keys: List[str]) -> None:
        if not new_keys:
            return
        if self.dry_run:
            logger.info("[DRY_RUN] mark_intraday_keys_sent(%s): %s", date_str, new_keys)
            return
        existing = self.get_sent_intraday_keys(date_str)
        merged = sorted(existing.union(new_keys))
        self._db.collection(COL_INTRADAY_STATE).document(date_str).set(
            {"date": date_str, "sent_keys": merged, "updated_at": vn_now().isoformat()},
            merge=True,
        )

    # -------------------- FCM Push Notifications --------------------
    def get_fcm_tokens(self) -> List[str]:
        if self.dry_run:
            return []
        doc = self._db.collection(COL_SETTINGS).document(DOC_FCM_TOKENS).get()
        if not doc.exists:
            return []
        tokens = (doc.to_dict() or {}).get("tokens", [])
        return [t for t in tokens if isinstance(t, str) and t.strip()]

    def _prune_invalid_tokens(self, invalid_tokens: List[str]) -> None:
        if not invalid_tokens or self.dry_run:
            return
        current = self.get_fcm_tokens()
        remaining = [t for t in current if t not in set(invalid_tokens)]
        self._db.collection(COL_SETTINGS).document(DOC_FCM_TOKENS).set(
            {"tokens": remaining, "updated_at": vn_now().isoformat()},
            merge=True,
        )
        logger.info("Pruned %d invalid FCM tokens", len(invalid_tokens))

    def send_fcm_notification(
        self,
        title: str,
        body: str,
        data: Optional[Dict[str, str]] = None,
    ) -> int:
        """Send push notification to all registered FCM tokens. Returns success count."""
        if self.dry_run:
            logger.info("[DRY_RUN] FCM Push | %s | %s", title, body)
            return 0

        tokens = self.get_fcm_tokens()
        if not tokens:
            logger.info("No FCM tokens registered in settings/fcm_tokens — skipping push.")
            return 0

        str_data = {str(k): str(v) for k, v in (data or {}).items()}
        message = self._messaging.MulticastMessage(
            notification=self._messaging.Notification(title=title, body=body),
            android=self._messaging.AndroidConfig(
                priority="normal",
                notification=self._messaging.AndroidNotification(
                    channel_id="swingbot_silent",
                    default_sound=False,
                    default_vibrate_timings=False,
                    default_light_settings=False,
                    notification_priority="PRIORITY_LOW",
                ),
            ),
            data=str_data,
            tokens=tokens,
        )

        try:
            response = self._messaging.send_each_for_multicast(message)
            logger.info(
                "FCM multicast sent: %d success, %d failure",
                response.success_count,
                response.failure_count,
            )
            if response.failure_count > 0:
                invalid_tokens = []
                for idx, resp in enumerate(response.responses):
                    if not resp.success:
                        err_code = getattr(resp.exception, "code", "")
                        if "registration-token-not-registered" in str(err_code).lower() or "invalid" in str(resp.exception).lower():
                            invalid_tokens.append(tokens[idx])
                if invalid_tokens:
                    self._prune_invalid_tokens(invalid_tokens)
            return int(response.success_count)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send FCM notification: %s", exc)
            return 0
