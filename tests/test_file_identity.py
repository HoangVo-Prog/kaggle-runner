import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kaggle_runner.file_identity import (
    FileIdentityError,
    file_identity,
    identity_matches,
    valid_file_identity,
)


class FileIdentityTests(unittest.TestCase):
    def test_native_windows_file_identity_on_temporary_file(self):
        if os.name != 'nt':
            self.skipTest('FILE_ID_INFO is a Windows API')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metadata-only.tmp'
            path.write_bytes(b'fixture')
            before = path.read_bytes()
            identity = file_identity(path)
            self.assertEqual(identity['version'], 2)
            self.assertEqual(identity['platform'], 'windows')
            self.assertEqual(len(identity['volume_serial']), 16)
            self.assertEqual(len(identity['file_id']), 32)
            self.assertTrue(valid_file_identity(identity))
            self.assertEqual(path.read_bytes(), before)

    def test_observed_64_bit_vs_32_bit_device_mismatch(self):
        canonical = {
            'version': 2,
            'platform': 'windows',
            'volume_serial': 'B880A9FD80A9C26C',
            'file_id': 'D215060000005A000000000000000000',
        }
        historical = {'device': 13294813006248133228, 'inode': 25332747904357842}
        truncated = {'device': 2158609004, 'inode': 25332747904357842}
        self.assertEqual(historical['device'] & 0xFFFFFFFF, truncated['device'])
        self.assertTrue(identity_matches(historical, canonical))
        self.assertFalse(identity_matches(truncated, canonical))

    def test_legacy_identity_requires_full_volume_and_file_id(self):
        current = {
            'version': 2,
            'platform': 'windows',
            'volume_serial': '0000000100000002',
            'file_id': '07000000000000000000000000000000',
        }
        self.assertTrue(identity_matches({'device': 0x100000002, 'inode': 7}, current))
        self.assertFalse(identity_matches({'device': 2, 'inode': 7}, current))

    def test_legacy_record_cannot_verify_itself_on_windows(self):
        if os.name != 'nt': self.skipTest('Windows legacy identity only')
        legacy={'device':13294813006248133228,'inode':25332747904357842}
        self.assertFalse(identity_matches(legacy,legacy))

    def test_same_inode_on_different_full_volume_is_not_same_identity(self):
        a = {'version': 2, 'platform': 'windows', 'volume_serial': '0000000100000002',
             'file_id': '07000000000000000000000000000000'}
        b = {'version': 2, 'platform': 'windows', 'volume_serial': '0000000200000002',
             'file_id': '07000000000000000000000000000000'}
        self.assertFalse(identity_matches(a, b))
        self.assertFalse(identity_matches({'device': 0x100000002, 'inode': 7}, b))

    def test_legacy_inode_cannot_match_nonzero_high_file_id_bits(self):
        current = {'version': 2, 'platform': 'windows', 'volume_serial': '0000000000000001',
                   'file_id': '07000000000000000100000000000000'}
        self.assertFalse(identity_matches({'device': 1, 'inode': 7}, current))

    def test_malformed_and_unsupported_records_are_rejected(self):
        good = {'version': 2, 'platform': 'windows', 'volume_serial': '0000000000000001',
                'file_id': '01000000000000000000000000000000'}
        malformed = [
            {'version': True, 'platform': 'windows', 'volume_serial': '0000000000000001',
             'file_id': '01000000000000000000000000000000'},
            good | {'extra': 1},
            good | {'volume_serial': '1'},
            {'device': True, 'inode': 1},
            {'version': 3, 'platform': 'windows', 'volume_serial': '0000000000000001',
             'file_id': '01000000000000000000000000000000'},
        ]
        for item in malformed:
            with self.subTest(item=item):
                self.assertFalse(valid_file_identity(item))
                self.assertFalse(identity_matches(item, good))

    def test_unsupported_windows_identity_retrieval_fails_closed(self):
        if os.name != 'nt':
            self.skipTest('FILE_ID_INFO is a Windows API')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metadata-only.tmp'
            path.write_bytes(b'fixture')
            with patch('kaggle_runner.file_identity._windows_file_identity',
                       side_effect=FileIdentityError('unsupported fixture')):
                with self.assertRaises(FileIdentityError):
                    file_identity(path)


if __name__ == '__main__':
    unittest.main()
