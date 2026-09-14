import io
import json
import os

# This legacy integration test intentionally exercises demo data, which is
# available only under the explicit development environment.
os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('SECRET_KEY', 'test-only-development-secret')

from app import app
import database

def run_tests():
    print("Initializing test database...")
    database.init_db()

    # Reset demo earner state for clean test run
    conn = database.get_db_connection()
    conn.execute("DELETE FROM submissions WHERE user_id = 2 AND task_id IN (4, 5)")
    conn.execute("UPDATE users SET wallet_balance = 140.0 WHERE id = 2")
    conn.execute("UPDATE settings SET value = '0' WHERE key = 'auto_approve_enabled'")
    conn.commit()
    conn.close()

    client = app.test_client()

    print("\n--- TEST 1: Serve Index HTML ---")
    res = client.get('/')
    assert res.status_code == 200, f"Failed index page: {res.status_code}"
    assert b"TaskPe" in res.data, "Brand TaskPe not found in index HTML"
    assert b"AI OCR" in res.data, "AI OCR banner not found in index HTML"
    print("PASS: Index HTML rendered successfully with AI OCR engine components.")

    print("\n--- TEST 2: Verify Task 4 Updated from 12 to 25 Target Numbers ---")
    res = client.get('/api/tasks/4')
    assert res.status_code == 200
    task4 = res.get_json()['task']
    assert len(task4['target_numbers']) == 25, f"Expected 25 numbers, got {len(task4['target_numbers'])}"
    assert "25 Numbers" in task4['title']
    print(f"PASS: Task #4 verified with {len(task4['target_numbers'])} target numbers ({task4['title']}).")

    print("\n--- TEST 3: Demo Earner Login ---")
    res = client.post('/api/auth/demo-login', json={'role': 'earner'})
    assert res.status_code == 200
    earner_data = res.get_json()
    assert earner_data['success'] is True
    assert earner_data['user']['role'] == 'user'
    earner_id = earner_data['user']['id']
    initial_balance = earner_data['user']['wallet_balance']
    print(f"PASS: Logged in as {earner_data['user']['name']} (Wallet: Rs. {initial_balance})")

    print("\n--- TEST 4: Submit Proof & Verify Automated AI OCR Audit Badge ---")
    fake_img = (io.BytesIO(b"dummy image bytes"), "proof_screenshot.png")
    res = client.post('/api/tasks/4/submit', data={
        'proof_image': fake_img,
        'notes': 'Sent to all 25 numbers using direct wa.me link'
    }, content_type='multipart/form-data')
    assert res.status_code == 200, f"Submit failed: {res.get_json()}"
    sub_data = res.get_json()
    assert sub_data['success'] is True
    submission_id = sub_data['submission_id']
    assert 'audit_badge' in sub_data
    assert "Auto-Verified" in sub_data['audit_badge'] or "Audit" in sub_data['audit_badge']
    print(f"PASS: Proof analyzed by OCR! Badge: '{sub_data['audit_badge']}' (Submission #{submission_id})")

    print("\n--- TEST 5: Admin Submissions Queue Displays Audit Tag ---")
    # Switch to admin
    client.post('/api/auth/demo-login', json={'role': 'admin'})
    res = client.get('/api/admin/submissions')
    assert res.status_code == 200
    subs = res.get_json()['submissions']
    sub_found = next((s for s in subs if s['id'] == submission_id), None)
    assert sub_found is not None
    assert sub_found['audit_badge'] is not None
    print(f"PASS: Submission found in admin queue with badge: '{sub_found['audit_badge']}'")

    print("\n--- TEST 6: Admin 1-Click Approve Submission ---")
    res = client.post(f'/api/admin/submissions/{submission_id}/approve')
    assert res.status_code == 200
    assert res.get_json()['success'] is True
    print(f"PASS: Submission #{submission_id} approved via 1-click.")

    print("\n--- TEST 7: Earner Wallet Credited with Task 4 Payout ---")
    client.post('/api/auth/demo-login', json={'role': 'earner'})
    res = client.get('/api/wallet/summary')
    assert res.status_code == 200
    wallet = res.get_json()['wallet']
    expected_bal = initial_balance + task4['payout']
    assert abs(wallet['balance'] - expected_bal) < 0.01, f"Expected {expected_bal}, got {wallet['balance']}"
    print(f"PASS: Earner wallet credited correctly: Rs. {wallet['balance']:.2f}")

    print("\n--- TEST 8: Test Auto-Approve Setting Toggle ---")
    client.post('/api/auth/demo-login', json={'role': 'admin'})
    res = client.post('/api/admin/settings/toggle-auto-approve')
    assert res.status_code == 200
    setting_res = res.get_json()
    assert 'auto_approve_enabled' in setting_res
    print(f"PASS: Auto-approval setting toggled to: {setting_res['auto_approve_enabled']}")

    print("\n--- TEST 9: Invalid Proof Cannot Be Auto-Approved ---")
    # Even with the setting enabled, a non-image/empty OCR result must stay
    # pending.  It must never credit a wallet based on submitter notes alone.
    if not setting_res['auto_approve_enabled']:
        client.post('/api/admin/settings/toggle-auto-approve')

    client.post('/api/auth/demo-login', json={'role': 'earner'})
    bal_before = client.get('/api/wallet/summary').get_json()['wallet']['balance']
    
    # Submit Task 5
    fake_img = (io.BytesIO(b"dummy image bytes"), "proof_status.png")
    res = client.post('/api/tasks/5/submit', data={
        'proof_image': fake_img,
        'notes': 'Uploaded WhatsApp status for luxury villa'
    }, content_type='multipart/form-data')
    assert res.status_code == 200, f"Submit failed: {res.get_json()}"
    auto_sub = res.get_json()
    assert auto_sub['auto_approved'] is False, "Invalid proof must never be auto-approved"
    
    bal_after = client.get('/api/wallet/summary').get_json()['wallet']['balance']
    assert abs(bal_after - bal_before) < 0.01, f"Invalid proof changed wallet balance: Before {bal_before}, After {bal_after}"
    print("PASS: Invalid proof was flagged and did not credit the earner.")

    print("\n=======================================================")
    print("ALL 9 AI/OCR AUDIT & 25-NUMBER SECURITY TESTS PASSED 100%!")
    print("=======================================================\n")

if __name__ == '__main__':
    run_tests()
