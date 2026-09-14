import unittest
from unittest.mock import patch

import ocr_scanner


TARGETS = ["919711223301", "919711223302"]
TEMPLATE = "Join the Slice and Spice Bistro grand opening this Friday"


class ScreenshotAuditTests(unittest.TestCase):
    def audit(self, ocr_text, notes=""):
        with patch.object(ocr_scanner, "run_ocr_on_image", return_value=ocr_text):
            return ocr_scanner.audit_screenshot_proof("uploaded-proof.png", TARGETS, TEMPLATE, notes)

    def test_photo_with_no_text_is_flagged_even_if_notes_claim_completion(self):
        result = self.audit("", "Sent to all 25 contacts; please approve")
        self.assertFalse(result["audit_passed"])
        self.assertEqual(result["numbers_found"], 0)
        self.assertIn("Audit Flagged", result["audit_badge"])

    def test_one_recipient_is_not_counted_as_bulk_delivery(self):
        result = self.audit(
            "WhatsApp\n919711223301\nJoin the Slice and Spice Bistro grand opening this Friday"
        )
        self.assertFalse(result["audit_passed"])
        self.assertEqual(result["numbers_found"], 1)

    def test_notes_cannot_supply_missing_recipients_or_message(self):
        result = self.audit("WhatsApp", "919711223301 919711223302 " + TEMPLATE)
        self.assertFalse(result["audit_passed"])
        self.assertEqual(result["numbers_found"], 0)

    def test_complete_messaging_proof_can_pass(self):
        result = self.audit(
            "WhatsApp Broadcast 919711223301 919711223302 " + TEMPLATE
        )
        self.assertTrue(result["audit_passed"])
        self.assertEqual(result["numbers_found"], 2)

    def test_whatsapp_invalid_number_is_excluded_from_required_count(self):
        result = self.audit(
            "WhatsApp The number +91 97112 23302 isn't on WhatsApp. "
            "919711223301 " + TEMPLATE
        )
        self.assertTrue(result["audit_passed"])
        self.assertEqual(result["invalid_count"], 1)
        self.assertEqual(result["total_required"], 1)
        self.assertEqual(result["numbers_found"], 1)

    def test_duplicate_target_detections_count_once(self):
        result = self.audit(
            "WhatsApp 919711223301 919711223301 919711223301 " + TEMPLATE
        )
        self.assertEqual(result["numbers_found"], 1)
        self.assertEqual(result["duplicate_detections"], 2)

    def test_three_different_assigned_targets_are_counted(self):
        targets = TARGETS + ["919711223303"]
        with patch.object(ocr_scanner, "run_ocr_on_image", return_value=(
            "WhatsApp Broadcast 919711223301 919711223302 919711223303 " + TEMPLATE
        )):
            result = ocr_scanner.audit_screenshot_proof("uploaded-proof.png", targets, TEMPLATE)
        self.assertEqual(result["numbers_found"], 3)
        self.assertEqual(result["completion_percentage"], 100.0)

    def test_unrelated_phone_numbers_do_not_count(self):
        result = self.audit("WhatsApp Broadcast 919700000000 " + TEMPLATE)
        self.assertEqual(result["numbers_found"], 0)
        self.assertEqual(result["unmatched_numbers"], TARGETS)

    def test_landing_page_is_not_sent_message_evidence(self):
        result = self.audit("Chat on WhatsApp\nOpen app\n919711223301")
        self.assertTrue(result["landing_page_detected"])
        self.assertFalse(result["message_evidence_detected"])
        self.assertEqual(result["numbers_found"], 0)
        self.assertFalse(result["audit_passed"])

    def test_content_match_does_not_replace_completion_count(self):
        targets = TARGETS + [f"919711223{number:03d}" for number in range(303, 326)]
        # The submitted proof still has one verified target out of the full
        # assignment, even though its message text matches perfectly.
        with patch.object(ocr_scanner, "run_ocr_on_image", return_value=(
            "WhatsApp Broadcast 919711223301 " + TEMPLATE
        )):
            result = ocr_scanner.audit_screenshot_proof("uploaded-proof.png", targets, TEMPLATE)
        self.assertEqual(result["content_match_score"], 100)
        self.assertEqual(result["numbers_found"], 1)
        self.assertEqual(result["total_required"], 25)
        self.assertEqual(result["completion_percentage"], 4.0)
        self.assertFalse(result["audit_passed"])

    def test_overlapping_targets_across_images_count_once(self):
        with patch.object(ocr_scanner, "run_ocr_on_image", side_effect=[
            "WhatsApp Broadcast 919711223301 " + TEMPLATE,
            "WhatsApp Broadcast 919711223301 919711223302 " + TEMPLATE,
        ]):
            result = ocr_scanner.audit_screenshot_proof(
                ["proof-1.png", "proof-2.png"], TARGETS, TEMPLATE
            )
        self.assertEqual(result["numbers_found"], 2)
        self.assertEqual(result["duplicate_detections"], 1)

    def test_notes_cannot_inflate_target_completion(self):
        targets = TARGETS + [f"919711223{number:03d}" for number in range(303, 326)]
        with patch.object(ocr_scanner, "run_ocr_on_image", return_value=(
            "WhatsApp Broadcast 919711223301 " + TEMPLATE
        )):
            result = ocr_scanner.audit_screenshot_proof("uploaded-proof.png", targets, TEMPLATE, "Sent to all 25")
        self.assertEqual(result["numbers_found"], 1)
        self.assertEqual(result["completion_percentage"], 4.0)


if __name__ == "__main__":
    unittest.main()
