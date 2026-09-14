import os
import re
import json
import difflib
import asyncio
import os

# Attempt to import Windows native OCR
HAS_WINSDK = False
try:
    import winsdk.windows.graphics.imaging as imaging
    import winsdk.windows.media.ocr as ocr
    import winsdk.windows.storage as storage
    HAS_WINSDK = True
except Exception as e:
    print(f"Native Windows SDK OCR not available: {e}")

async def _extract_text_native_async(file_path):
    """Run native Windows.Media.Ocr on the given image path."""
    abs_path = os.path.abspath(file_path)
    file = await storage.StorageFile.get_file_from_path_async(abs_path)
    stream = await file.open_async(storage.FileAccessMode.READ)
    decoder = await imaging.BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = ocr.OcrEngine.try_create_from_user_profile_languages()
    if not engine:
        # Fallback to English
        from winsdk.windows.globalization import Language
        engine = ocr.OcrEngine.try_create_from_language(Language("en-US"))
    if not engine:
        return ""
    result = await engine.recognize_async(bitmap)
    return result.text or ""

def run_ocr_on_image(file_path):
    """Extract text from screenshot using native Windows OCR or regex fallback."""
    if not os.path.exists(file_path):
        return ""
    
    if HAS_WINSDK:
        try:
            return asyncio.run(_extract_text_native_async(file_path))
        except Exception:
            # Non-image or corrupted format, fallback gracefully
            return ""
    return ""


def _normalize_phone(value):
    """Return the comparable 10-digit phone form for a displayed number."""
    digits = re.sub(r'\D', '', str(value))
    return digits[-10:] if len(digits) >= 10 else digits


def _find_invalid_numbers(ocr_text, target_numbers):
    """Find target recipients rejected by WhatsApp in the supplied screenshot."""
    target_by_last10 = {_normalize_phone(number): str(number) for number in target_numbers}
    invalid = []
    pattern = re.compile(
        r"(?:the\s+)?number\s*(\+?[\d\s\-\(\)]{8,})\s*(?:isn['’]t|is\s+not)\s+on\s+whatsapp",
        re.IGNORECASE,
    )
    for match in pattern.finditer(ocr_text):
        target = target_by_last10.get(_normalize_phone(match.group(1)))
        if target and target not in invalid:
            invalid.append(target)
    return invalid


def _assigned_number_matches(ocr_text, target_numbers):
    """Return unique assigned targets and repeated detections from OCR text."""
    clean_corpus = re.sub(r'\D', '', ocr_text)
    target_by_last10 = {}
    for number in target_numbers:
        normalized = _normalize_phone(number)
        if normalized:
            target_by_last10.setdefault(normalized, str(number))

    matched = []
    duplicate_detections = 0
    for normalized, original in target_by_last10.items():
        occurrences = clean_corpus.count(normalized)
        if occurrences:
            matched.append(original)
            duplicate_detections += max(0, occurrences - 1)
    return matched, duplicate_detections

def audit_screenshot_proof(image_path, target_numbers, template_text, notes=""):
    """
    Audits the uploaded screenshot:
    1. Extracts text using OCR.
    2. Searches for target numbers (normalized matching).
    3. Calculates template text similarity percentage.
    4. Produces audit badge, score, and auto-approval recommendation.
    """
    image_paths = image_path if isinstance(image_path, (list, tuple)) else [image_path]
    ocr_text = "\n".join(run_ocr_on_image(path) for path in image_paths)
    
    # Evidence must come from the uploaded image.  Submitter notes are useful to
    # an admin, but are untrusted claims and must never influence an audit.
    search_corpus = ocr_text.lower()
    unique_targets = []
    seen_targets = set()
    for number in target_numbers:
        normalized = _normalize_phone(number)
        if normalized and normalized not in seen_targets:
            seen_targets.add(normalized)
            unique_targets.append(str(number))

    total_target = len(unique_targets)
    invalid_numbers = _find_invalid_numbers(ocr_text, target_numbers)
    invalid_number_set = {_normalize_phone(number) for number in invalid_numbers}
    effective_total_required = max(0, total_target - len(invalid_number_set))
    found_numbers, duplicate_detections = _assigned_number_matches(ocr_text, unique_targets)
    found_numbers = [number for number in found_numbers if _normalize_phone(number) not in invalid_number_set]

    landing_page_detected = bool(re.search(
        r'chat\s+on\s+whatsapp|open\s+app|continue\s+to\s+whatsapp\s+web|use\s+whatsapp\s+web',
        search_corpus,
    ))
    explicit_message_evidence = bool(re.search(
        r'\b(?:sent|delivered|broadcast|forwarded|you\s*:|double\s+check|read)\b',
        search_corpus,
    ))
    message_context = bool(re.search(r'whatsapp|broadcast|forwarded', search_corpus))
    message_evidence_detected = explicit_message_evidence or (message_context and not landing_page_detected)
    # A landing/opening page can show a target number without proving that a
    # message was sent. It contributes no verified targets unless the same OCR
    # also contains an independent message-delivery indicator.
    if landing_page_detected and not message_evidence_detected:
        found_numbers = []

    numbers_found_count = len({_normalize_phone(number) for number in found_numbers})

    # Template similarity calculation
    # Clean template words (length > 3)
    clean_template = re.sub(r'[^\w\s]', ' ', template_text).lower()
    template_words = [w for w in clean_template.split() if len(w) > 3]

    if template_words:
        matched_words = [w for w in template_words if w in search_corpus]
        word_match_ratio = len(matched_words) / len(template_words)
    else:
        word_match_ratio = 1.0

    # Sequence matcher for overall phrase match
    seq_ratio = difflib.SequenceMatcher(None, template_text.lower()[:200], ocr_text.lower()[:200]).ratio()
    match_percentage = int(max(word_match_ratio * 100, seq_ratio * 100))

    # Cap match percentage at 100
    match_percentage = min(100, max(0, match_percentage))

    # Automatic approval is deliberately strict.  A bulk-send proof must show
    # every required recipient and a strong match to the required message.  OCR
    # text must also include a WhatsApp/broadcast indicator, preventing arbitrary
    # photographs or unrelated screenshots from being treated as proof.
    has_ocr_evidence = bool(ocr_text.strip())
    has_messaging_context = bool(re.search(r'whatsapp|broadcast|forwarded|message', search_corpus))
    all_recipients_found = (
        effective_total_required > 0
        and numbers_found_count == effective_total_required
    )
    template_matches = match_percentage >= 85
    audit_passed = (
        has_ocr_evidence
        and has_messaging_context
        and message_evidence_detected
        and not (landing_page_detected and not message_evidence_detected)
        and all_recipients_found
        and template_matches
    )

    failure_reasons = []
    if not has_ocr_evidence:
        failure_reasons.append("No readable screenshot text detected")
    if has_ocr_evidence and not has_messaging_context:
        failure_reasons.append("No WhatsApp or broadcast context detected")
    if landing_page_detected and not message_evidence_detected:
        failure_reasons.append("WhatsApp landing page does not prove a sent message")
    if not all_recipients_found:
        failure_reasons.append(f"Only {numbers_found_count}/{effective_total_required} required recipients detected")
    if not template_matches:
        failure_reasons.append("Message text does not sufficiently match the task template")

    # Format Badge
    if audit_passed:
        audit_badge = f"Auto-Verified: {numbers_found_count}/{effective_total_required} valid numbers found (Match: {match_percentage}%)"
    else:
        audit_badge = f"Audit Flagged: {numbers_found_count}/{effective_total_required} valid numbers found (Match: {match_percentage}%)"

    return {
        "audit_passed": audit_passed,
        "audit_badge": audit_badge,
        "numbers_found": numbers_found_count,
        "total_required": effective_total_required,
        "total_targets": total_target,
        "completion_percentage": round((numbers_found_count / effective_total_required) * 100, 2) if effective_total_required else 0,
        "invalid_numbers": invalid_numbers,
        "invalid_count": len(invalid_numbers),
        "unmatched_numbers": [number for number in unique_targets if _normalize_phone(number) not in {_normalize_phone(item) for item in found_numbers} and _normalize_phone(number) not in invalid_number_set],
        "duplicate_detections": duplicate_detections,
        "landing_page_detected": landing_page_detected,
        "message_evidence_detected": message_evidence_detected,
        "content_match_score": match_percentage,
        "match_percentage": match_percentage,
        "matched_numbers": found_numbers,
        "ocr_extracted_text": ocr_text[:500],
        "failure_reasons": failure_reasons
    }
