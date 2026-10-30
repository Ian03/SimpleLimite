import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app_paths import cursor_config_dir, user_data_dir


class LinuxPathsTests(unittest.TestCase):
    def test_xdg_directories(self):
        with patch('app_paths.sys.platform', 'linux'), patch.dict(os.environ, {
            'XDG_CONFIG_HOME': '/tmp/settings', 'XDG_DATA_HOME': '/tmp/data',
            'LOCALAPPDATA': '',
        }):
            self.assertEqual(cursor_config_dir(), Path('/tmp/settings/Cursor'))
            self.assertEqual(user_data_dir(), Path('/tmp/data/SimpleLimite'))

    def test_linux_defaults(self):
        with patch('app_paths.sys.platform', 'linux'), patch.dict(os.environ, {
            'XDG_CONFIG_HOME': '', 'XDG_DATA_HOME': '', 'LOCALAPPDATA': '',
        }):
            self.assertEqual(cursor_config_dir(), Path.home() / '.config/Cursor')
            self.assertEqual(user_data_dir(), Path.home() / '.local/share/SimpleLimite')

    def test_windows_cursor(self):
        with patch('app_paths.sys.platform', 'win32'), patch.dict(os.environ, {'APPDATA': 'C:/settings'}):
            self.assertEqual(cursor_config_dir(), Path('C:/settings/Cursor'))
