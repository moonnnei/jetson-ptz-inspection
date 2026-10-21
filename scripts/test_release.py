"""Regressions for release path, secret and manifest guards. No hardware."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from check_release import audit, public_name


class ReleaseGuardTests(unittest.TestCase):
    def make_tree(self, root):
        contents = {
            'LICENSE': b'GNU AFFERO GENERAL PUBLIC LICENSE',
            'README.md': b'# Test\n', 'CONTRIBUTING.md': b'# Contributing\n',
            'SECURITY.md': b'# Security\n', 'THIRD_PARTY_NOTICES.md': b'# Notices\n',
            '.github/workflows/ci.yml': b'name: Test\n', 'docs/PUBLISHING.md': b'# Publish\n',
            'config/sample.example.json': b'{"user":"YOUR_CAMERA_USER","camera":"http://192.0.2.1"}',
        }
        records = []
        for name, data in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            records.append(dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        (root / 'FILE_MANIFEST.json').write_text(json.dumps(dict(files=records)), encoding='utf-8')

    def test_private_and_escaping_paths_cannot_be_added(self):
        for name in ('../outside.py', '/absolute.py', 'var/run.json', '.ssh/key',
                     'config/camera.local.json', 'scene.jpg', '.env', 'C:/secret.py',
                     'models/best.pt','models/model.onnx','registry.sqlite3','camera.db'):
            with self.assertRaises(ValueError):
                public_name(name)
        self.assertEqual(public_name('.github/workflows/ci.yml'), '.github/workflows/ci.yml')

    def test_modified_and_unlisted_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_tree(root)
            self.assertEqual(audit(root, True), [])
            (root / 'README.md').write_bytes(b'changed\n')
            (root / 'unexpected.py').write_bytes(b'print(1)\n')
            errors = audit(root, True)
            self.assertTrue(any('Manifest mismatch: README.md' in e for e in errors))
            self.assertTrue(any('Unlisted public/tracked file: unexpected.py' in e for e in errors))

    def test_secret_detection_does_not_echo_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_tree(root)
            secret = 'gh' + 'p_' + 'A' * 36
            (root / 'README.md').write_text(secret, encoding='utf-8')
            errors = audit(root)
            self.assertTrue(any('Possible github-token' in e for e in errors))
            self.assertFalse(any(secret in e for e in errors))

    def test_credentials_and_broken_links_fail_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_tree(root)
            (root / 'config/sample.example.json').write_text(
                json.dumps(dict(user='actual-user', camera='http://user:example@192.0.2.1')), encoding='utf-8')
            (root / 'README.md').write_text('[missing](missing.md)\n', encoding='utf-8')
            errors = audit(root)
            self.assertTrue(any('username is not a placeholder' in e for e in errors))
            self.assertTrue(any('Credentials in example URL' in e for e in errors))
            self.assertTrue(any('Broken local Markdown link' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
