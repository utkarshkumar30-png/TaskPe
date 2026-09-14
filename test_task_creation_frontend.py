from pathlib import Path
import json
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent


class TaskCreationFrontendRegressionTests(unittest.TestCase):
    def test_api_rendering_escapes_all_stored_xss_fields(self):
        source = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        for expression in (
            'escapeHtml(t.description)', 'escapeHtml(t.created_at)',
            'escapeHtml(w.payout_method)', 'escapeHtml(w.upi_id || \'Bank\')',
            'escapeHtml(w.requested_at)', 'escapeHtml(w.utr_number)',
            'escapeHtml(w.provider_reference)', 'escapeHtml(w.transaction_hash)',
            'escapeHtml(s.user_name)', 'escapeHtml(s.user_mobile)',
            'escapeHtml(s.task_title)', 'escapeHtml(s.submitted_at)',
            'escapeHtml(t.title)', 'escapeHtml(t.category)', 'escapeHtml(t.status)',
            'escapeHtml(u.name)', 'escapeHtml(u.mobile)', 'escapeHtml(u.email)',
            'escapeHtml(f.name)', 'escapeHtml(f.mobile)', 'escapeHtml(u.name)',
            'escapeHtml(u.mobile)', 'messageNode.textContent = String(message ?? \'\')',
        ):
            self.assertIn(expression, source)
        for unsafe in (
            '${t.description}', '${w.user_name}', '${t.title}', '${u.name}',
            '${f.name}', '${u.mobile}',
        ):
            self.assertNotIn(unsafe, source)
        self.assertIn('onclick="openSubmissionLightboxFromButton(this)"', source)
        self.assertIn('onclick="approveWithdrawalFromButton(this)"', source)

    def test_escape_html_never_leaves_xss_payload_as_markup(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is not installed')
        helper = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        start = helper.index('function escapeHtml')
        end = helper.index('\n}\n', start) + 2
        payloads = [
            '<script>alert(1)</script>',
            '<img src=x onerror=alert(1)>',
            '"><svg/onload=alert(1)>',
        ]
        script = f"""
{helper[start:end]}
const payloads = {json.dumps(payloads)};
for (const payload of payloads) {{
  const output = escapeHtml(payload);
  if (output.includes('<script') || output.includes('<img') || output.includes('<svg')) process.exit(1);
  if (!output.includes('&lt;') || !output.includes('&gt;')) process.exit(2);
}}
"""
        result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_task_cards_escape_untrusted_task_fields(self):
        source = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        render_block = source[source.index('function renderTasksGrid'):source.index('async function startTask')]
        self.assertIn('function escapeHtml(value)', source)
        for field in ('category', 'title', 'description', 'time_limit_minutes', 'target_count',
                      'total_slots', 'completed_slots'):
            self.assertIn(f'escapeHtml(task.{field})', render_block)
        self.assertIn('escapeHtml(task.available_slots ?? task.slots_remaining)', render_block)
        self.assertIn('escapeHtml(task.taken_slots ??', render_block)
        self.assertNotIn('${task.category}', render_block)
        self.assertNotIn('${task.title}', render_block)
        self.assertNotIn('${task.description}', render_block)
        self.assertNotIn('${task.time_limit_minutes}', render_block)

    def test_admin_creation_has_single_submit_handler_and_inflight_guard(self):
        source = (ROOT / 'static' / 'js' / 'admin.js').read_text(encoding='utf-8')
        self.assertEqual(source.count("$('#task-form').addEventListener('submit'"), 1)
        self.assertIn('let taskCreateInFlight = false', source)
        self.assertIn('taskCreateInFlight = true', source)
        self.assertIn("submitButton.disabled = true", source)
        self.assertIn("submitButton.textContent = 'Publishing…'", source)
        self.assertIn("/api/admin/tasks/create", source)

    def test_both_creation_flows_keep_confirmation_and_wheel_protection(self):
        for filename in ('admin.js', 'app.js'):
            source = (ROOT / 'static' / 'js' / filename).read_text(encoding='utf-8')
            self.assertIn("addEventListener('wheel'", source)
            self.assertIn('Publish this task?', source)
            self.assertIn('Required numbers', source)
            self.assertIn('Target numbers pasted', source)


if __name__ == '__main__':
    unittest.main()
