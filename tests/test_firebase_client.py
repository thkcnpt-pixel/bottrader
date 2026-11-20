"""Unit tests for FirebaseClient FCM notification payload construction."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from firebase_admin import messaging
from firebase_admin._messaging_encoder import MessageEncoder

from utils.firebase_client import FirebaseClient


class TestFirebaseClientFCM(unittest.TestCase):
    def test_send_fcm_notification_builds_valid_android_payload(self):
        client = object.__new__(FirebaseClient)
        client.dry_run = False
        client._messaging = SimpleNamespace(
            MulticastMessage=messaging.MulticastMessage,
            Notification=messaging.Notification,
            AndroidConfig=messaging.AndroidConfig,
            AndroidNotification=messaging.AndroidNotification,
            send_each_for_multicast=MagicMock(
                return_value=SimpleNamespace(success_count=1, failure_count=0, responses=[])
            ),
        )
        client.get_fcm_tokens = MagicMock(return_value=["dummy-fcm-token"])

        sent = client.send_fcm_notification(
            title="Test Title",
            body="Test Body",
            data={"type": "daily_report", "date": "2026-11-20"},
        )

        self.assertEqual(sent, 1)
        client._messaging.send_each_for_multicast.assert_called_once()
        multicast_msg = client._messaging.send_each_for_multicast.call_args.args[0]

        # Encode as a single Message using firebase-admin's real encoder to verify schema validity
        single_msg = messaging.Message(
            notification=multicast_msg.notification,
            android=multicast_msg.android,
            data=multicast_msg.data,
            token=multicast_msg.tokens[0],
        )
        encoded = json.loads(json.dumps(single_msg, cls=MessageEncoder))
        android_notif = encoded["android"]["notification"]
        self.assertEqual(android_notif["channel_id"], "swingbot_silent")
        self.assertEqual(android_notif["notification_priority"], "PRIORITY_LOW")
        self.assertIs(android_notif["default_sound"], False)
        self.assertIs(android_notif["default_vibrate_timings"], False)
        self.assertIs(android_notif["default_light_settings"], False)

    def test_send_fcm_notification_handles_message_build_error_gracefully(self):
        client = object.__new__(FirebaseClient)
        client.dry_run = False
        client._messaging = SimpleNamespace(
            MulticastMessage=MagicMock(side_effect=TypeError("bad kwargs")),
            Notification=messaging.Notification,
            AndroidConfig=messaging.AndroidConfig,
            AndroidNotification=messaging.AndroidNotification,
            send_each_for_multicast=MagicMock(),
        )
        client.get_fcm_tokens = MagicMock(return_value=["dummy-fcm-token"])

        sent = client.send_fcm_notification(title="T", body="B")
        self.assertEqual(sent, 0)
        client._messaging.send_each_for_multicast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
