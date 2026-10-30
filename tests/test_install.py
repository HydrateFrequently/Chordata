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
                        KLIPPY_ENV=str(self.base / 'env'), CHORDATA_ALLOW_ROOT='1', CHORDATA_OFFLINE='1')

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

    def test_branch_line_with_a_comment_symlink_and_mode_kept(self):
        # "primary_branch: x  # note" is replaced, never doubled (Moonraker refuses a duplicate option);
        # a moonraker.conf that is a link stays a link, and keeps its mode
        branch = subprocess.run(['git', '-C', str(ROOT), 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                                text=True, capture_output=True).stdout.strip() or 'main'
        real = self.base / 'real-moonraker.conf'
        real.write_text('[server]\n\n[update_manager chordata]\ntype: git_repo\nprimary_branch = old  # stable\n')
        real.chmod(0o640)
        conf = self.config / 'moonraker.conf'
        conf.unlink()
        conf.symlink_to(real)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue(conf.is_symlink())
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)
        text = real.read_text()
        self.assertEqual(text.count('primary_branch'), 1, text)
        self.assertIn('primary_branch: %s\n' % branch, text)

    def test_include_already_there_with_a_comment_or_elsewhere(self):
        (self.config / 'extra').mkdir()
        (self.config / 'extra/macros.cfg').write_text('[include ../chordata_settings.cfg]\n')
        self.printer.write_text('[include chordata.cfg]  # belts\n[include extra/macros.cfg]\n[printer]\n')
        (self.config / 'extra/macros.cfg').write_text('[include chordata_settings.cfg]\n')
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.printer.read_text().count('chordata.cfg]'), 1)
        self.assertNotIn('chordata_settings.cfg', self.printer.read_text())

    def test_include_only_in_a_save_config_backup_is_added(self):
        # (Klipper's SAVE_CONFIG backups sit beside printer.cfg; only what printer.cfg includes counts)
        (self.config / 'printer-20261001_120000.cfg').write_text('[include chordata.cfg]\n[include chordata_settings.cfg]\n')
        self.printer.write_text('[printer]\nkinematics: corexy\n#*# <---------------------- SAVE_CONFIG ---------------------->\n')
        self.assertEqual(self.run_installer().returncode, 0)
        text = self.printer.read_text()
        self.assertTrue(text.startswith('[include chordata.cfg]\n[include chordata_settings.cfg]\n'), text)
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.config / 'chordata.cfg').exists())
        self.assertNotIn('left chordata.cfg in place', result.stdout)

    def test_uninstall_removes_the_whole_section_with_a_comment_in_it(self):
        conf = self.config / 'moonraker.conf'
        self.assertEqual(self.run_installer().returncode, 0)
        text = conf.read_text().replace('[update_manager chordata]\n', '[update_manager chordata]\n# pinned by me\n')
        conf.write_text('[server]\n' + text.split('[server]\n', 1)[1] + '\n[update_manager other]\nprimary_branch: keep\n')
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = conf.read_text()
        self.assertNotIn('chordata', out)
        self.assertNotIn('pinned by me', out)
        self.assertNotIn('managed_services', out)
        self.assertIn('[update_manager other]\nprimary_branch: keep\n', out)

    def test_uninstall_undoes_install(self):
        theme = self.config / '.theme'
        theme.mkdir()
        nav = [{'title': 'Camera', 'href': '/camera'}]
        (theme / 'navi.json').write_text(json.dumps(nav))
        conf = self.config / 'moonraker.conf'
        conf.write_text('[server]\nhost: 0.0.0.0\n\n[update_manager other]\nprimary_branch: keep\n')
        before_printer, before_conf = self.printer.read_text(), conf.read_text()
        self.assertEqual(self.run_installer().returncode, 0)
        (self.base / 'klipper/klippy/extras/other.py').write_text('# not ours\n')
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.printer.read_text(), before_printer)
        self.assertNotIn('chordata', conf.read_text())
        self.assertIn('[update_manager other]\nprimary_branch: keep\n', conf.read_text())
        self.assertIn('host: 0.0.0.0', conf.read_text())
        self.assertEqual(json.loads((theme / 'navi.json').read_text()), nav)
        self.assertFalse((self.config / 'chordata.cfg').exists())
        self.assertFalse((self.base / 'klipper/klippy/extras/chordata.py').exists())
        self.assertFalse((self.base / 'moonraker/moonraker/components/chordata_web.py').exists())
        self.assertTrue((self.base / 'klipper/klippy/extras/other.py').exists())
        self.assertTrue((self.config / 'chordata_settings.cfg').exists())     # the user's settings stay
        # a second run changes nothing and doesn't fail
        self.assertEqual(subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env).returncode, 0)
        self.assertEqual(self.printer.read_text(), before_printer)

    def test_uninstall_keeps_what_is_not_ours(self):
        conf_real = self.base / 'real-moonraker.conf'
        conf_real.write_text('[server]\n')
        conf = self.config / 'moonraker.conf'
        conf.unlink()
        conf.symlink_to(conf_real)
        self.assertEqual(self.run_installer().returncode, 0)
        # the user edits: a comment on an include, Windows line endings, a comment and section after ours
        text = self.printer.read_text().replace('[include chordata.cfg]', '[include chordata.cfg]  # belts')
        self.printer.write_bytes(text.replace('\n', '\r\n').encode())
        conf_real.write_text(conf_real.read_text() + '# Crowsnest webcam updates\n[update_manager crowsnest]\ntype: git_repo\n')
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.printer.read_bytes(), b'[printer]\r\nkinematics: corexy\r\n#*# SAVE_CONFIG\r\n')
        self.assertFalse((self.config / 'chordata.cfg').exists())
        self.assertTrue(conf.is_symlink())
        self.assertEqual(conf_real.read_text(), '[server]\n# Crowsnest webcam updates\n[update_manager crowsnest]\ntype: git_repo\n')

    def test_uninstall_strips_includes_from_the_file_that_holds_them(self):
        # the includes live in a file printer.cfg includes: install accepts them there, and
        # uninstall must take them out of that file (Klipper would not start with [chordata]
        # included once the plugin is gone)
        (self.config / 'macros.cfg').write_text('[include chordata.cfg]\n[include  chordata_settings.cfg ]  # belts\n[gcode_macro M117]\ngcode:\n')
        self.printer.write_text('[include macros.cfg]\n[printer]\nkinematics: corexy\n')
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertNotIn('chordata', self.printer.read_text())
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.config / 'macros.cfg').read_text(), '[gcode_macro M117]\ngcode:\n')
        self.assertTrue((self.config / 'macros.cfg.before-chordata-uninstall').exists())
        self.assertFalse((self.base / 'klipper/klippy/extras/chordata.py').exists())
        self.assertFalse((self.config / 'chordata.cfg').exists())

    def test_uninstall_refuses_while_an_unknown_include_form_remains(self):
        # an include this script can't strip (a glob): nothing is removed, so Klipper keeps starting
        self.assertEqual(self.run_installer().returncode, 0)
        self.printer.write_text(self.printer.read_text().replace('[include chordata_settings.cfg]', '[include chordata_*.cfg]'))
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('still includes', result.stderr)
        self.assertTrue((self.base / 'klipper/klippy/extras/chordata.py').is_symlink())
        self.assertTrue((self.config / 'chordata.cfg').is_symlink())
        self.assertNotIn('[include chordata.cfg]', self.printer.read_text())     # the known one went

    def test_an_indented_include_does_not_count(self):
        # (Klipper reads an indented line as part of the option before it, not as an include)
        self.printer.write_text('[printer]\nkinematics: corexy\n  [include chordata.cfg]\n')
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue(self.printer.read_text().startswith('[include chordata.cfg]\n[include chordata_settings.cfg]\n'))

    def test_switching_klipper_to_kalico_moves_the_plugin_link(self):
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue((self.base / 'klipper/klippy/extras/chordata.py').is_symlink())
        (self.base / 'klipper/klippy/plugins').mkdir()        # the same folder is now a Kalico
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue((self.base / 'klipper/klippy/plugins/chordata.py').is_symlink())
        self.assertTrue((self.base / 'klipper/klippy/plugins/chordata_belts.py').is_symlink())
        self.assertFalse((self.base / 'klipper/klippy/extras/chordata.py').exists())
        self.assertFalse((self.base / 'klipper/klippy/extras/chordata_belts.py').exists())

    def test_uninstall_keeps_a_symlinked_navigation_file_a_link(self):
        theme = self.config / '.theme'
        theme.mkdir()
        real = self.base / 'real-navi.json'
        real.write_text('[]')
        (theme / 'navi.json').symlink_to(real)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue((theme / 'navi.json').is_symlink())
        self.assertIn('Chordata', real.read_text())
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((theme / 'navi.json').is_symlink())
        self.assertEqual(json.loads(real.read_text()), [])

    def test_indented_update_options_keep_their_indent(self):
        conf = self.config / 'moonraker.conf'
        conf.write_text('[server]\n\n[update_manager chordata]\n    type: git_repo\n    path: ~/Chordata\n    origin: x\n\n[other]\nk: v\n')
        self.assertEqual(self.run_installer().returncode, 0)
        text = conf.read_text()
        self.assertRegex(text, r'\[update_manager chordata\]\n    primary_branch: \S+\n    type: git_repo\n')
        # a second run, with a branch line to change, keeps the indent too
        text = text.replace('primary_branch: ', 'primary_branch: nope  # was ')
        conf.write_text(text)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertRegex(conf.read_text(), r'\n    primary_branch: \S+\n    type: git_repo\n')
        self.assertNotIn('nope', conf.read_text())

    def test_printer_cfg_is_backed_up_again_after_a_reinstall(self):
        self.assertEqual(self.run_installer().returncode, 0)
        subprocess.run(['bash', str(ROOT / 'uninstall.sh')], env=self.env, capture_output=True)
        self.printer.write_text(self.printer.read_text() + '[fan]\npin: PA0\n')
        self.assertEqual(self.run_installer().returncode, 0)
        backups = sorted(p.name for p in self.config.glob('printer.cfg.before-chordata*') if not p.name.endswith('-uninstall'))
        self.assertEqual(len(backups), 2, backups)
        self.assertIn('[fan]', (self.config / backups[1]).read_text())
        self.assertNotIn('[fan]', (self.config / backups[0]).read_text())

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
