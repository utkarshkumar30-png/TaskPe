import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class WhatsAppDeepLinkTests(unittest.TestCase):
    def test_builder_normalizes_phone_and_encodes_prepared_message(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is not installed')
        source = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        start = source.index('function buildWhatsAppUrl')
        end = source.index('\n}', start) + 2
        message = 'Offer text\n\nhttps://example.com/deal?code=SAVE 50'
        script = f'''{source[start:end]}
const url = buildWhatsAppUrl('+91 98765-43210', {json.dumps(message)});
if (url !== 'https://wa.me/919876543210?text=' + encodeURIComponent({json.dumps(message)})) process.exit(1);
if (url.includes('image=') || url.includes('\\\\')) process.exit(2);
if (buildWhatsAppUrl('', 'message') !== null) process.exit(3);
'''
        result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recipient_action_is_a_normal_whatsapp_link_and_preserves_manual_image_flow(self):
        source = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('href="${waUrl}"', source)
        self.assertIn('<span>Open WhatsApp</span>', source)
        self.assertIn('encodeURIComponent(String(message ?? \'\'))', source)
        self.assertNotIn('&image=', source)
        self.assertIn('Attach the campaign image before sending.', (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
