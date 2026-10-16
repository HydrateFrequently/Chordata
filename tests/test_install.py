"""Installer behavior against temporary printer directories; no host service changes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / 'config'
        self.config.mkdir()
        self.printer = self.config / 'printer.cfg'
        self.printer.write_text('[printer]\nkinematics: corexy\n#*# SAVE_CONFIG\n')
        (self.config / 'moonraker.conf').write_text('[server]\n')
        (self.base / 'klipper/klippy/extras').mkdir(parents=True)
        (self.base / 'moonraker/moonraker/components').mkdir(parents=True)
        self.env = dict(os.environ, KLIPPER_DIR=str(self.base / 'klipper'),
                        MOONRAKER_DIR=str(self.base / 'moonraker'), CONFIG_DIR=str(self.config),
                        KLIPPY_ENV=str(self.base / 'env'), CHORDATA_ALLOW_ROOT='1')

    def run_installer(self):
        return subprocess.run(['bash', str(ROOT / 'install.sh')], env=self.env,
                              text=True, capture_output=True)

    def test_idempotent_preserves_user_settings_and_navigation(self):
        theme = self.config / '.theme'
        theme.mkdir()
        nav = [{'title': 'Camera', 'href': '/camera'}]
        (theme / 'navi.json').write_text(json.dumps(nav))
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.printer.read_text().count('[include chordata.cfg]'), 1)
        self.assertTrue(self.printer.read_text().endswith('#*# SAVE_CONFIG\n'))
        self.assertEqual(json.loads((theme / 'navi.json.before-chordata').read_text()), nav)
        self.assertEqual(len(json.loads((theme / 'navi.json').read_text())), 2)
        self.assertEqual((self.config / 'moonraker.conf').read_text().count('[chordata_web]'), 1)
        self.assertIn('origin: https://github.com/HydrateFrequently/Chordata.git', (self.config / 'moonraker.conf').read_text())
        settings = self.config / 'chordata_settings.cfg'
        settings.write_text('[chordata]\nbelt_target: 95\n')
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertIn('belt_target: 95', settings.read_text())

    def test_update_entry_follows_checked_out_branch(self):
        branch = subprocess.run(['git', '-C', str(ROOT), 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                                text=True, capture_output=True).stdout.strip() or 'main'
        conf = self.config / 'moonraker.conf'
        conf.write_text('[server]\n\n[update_manager chordata]\ntype: git_repo\n'
                        'primary_branch: some-old-branch\n\n[update_manager other]\nprimary_branch: keep\n')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = conf.read_text()
        self.assertIn('[update_manager chordata]\ntype: git_repo\nprimary_branch: %s\n' % branch, text)
        self.assertIn('[update_manager other]\nprimary_branch: keep\n', text)
        self.assertIn('some-old-branch', (self.config / 'moonraker.conf.before-chordata-branch').read_text())
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(conf.read_text(), text)

    def test_invalid_navigation_fails_before_changes(self):
        (self.config / '.theme').mkdir()
        (self.config / '.theme/navi.json').write_text('{broken')
        before = self.printer.read_text()
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.printer.read_text(), before)
        self.assertFalse((self.config / 'chordata.cfg').exists())

    def test_regular_plugin_file_is_not_overwritten(self):
        plugin = self.base / 'klipper/klippy/extras/chordata.py'
        plugin.write_text('# local custom file\n')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(plugin.read_text(), '# local custom file\n')

    def test_kalico_plugin_directory(self):
        (self.base / 'klipper/klippy/plugins').mkdir()
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue((self.base / 'klipper/klippy/plugins/chordata.py').is_symlink())


if __name__ == '__main__':
    unittest.main()
