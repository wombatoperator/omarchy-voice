"""Discovery must reflect installed APIs and overrides without running actions."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omarchy_voice import capabilities as cap, discovery as d
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor, _desktop_entry_exists, desktop_actions


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_every_public_group_is_discoverable_with_privilege_metadata(self):
        entries = [dict(route=f"omarchy {group} example", group=group,
                        requires_sudo=(group == "pkg"))
                   for group in ("plugin", "hook", "pkg", "new-future-group")]
        entries.append(dict(route="omarchy private", hidden=True))
        for data in (entries, {"commands": entries}):
            with mock.patch.object(cap, "_run", return_value=json.dumps(data)):
                rows = cap.command_index()
            self.assertEqual(len(rows), 4)
            self.assertIn("administrator", dict(rows)["omarchy pkg example"])

    def test_invalid_catalogue_does_not_crash(self):
        for data in ("", "invalid", "null", "42", '{"commands":null}', '[null, {}, {"route": 1}]'):
            with mock.patch.object(cap, "_run", return_value=data):
                self.assertEqual(cap.command_catalogue(), [])

    def test_catalogue_updates_are_not_hidden_by_stale_cache(self):
        with mock.patch.object(cap, "_run", side_effect=["", '[{"route":"omarchy new"}]']):
            self.assertEqual(cap.command_index(), [])
            self.assertEqual(cap.command_index(), [("omarchy new", "")])

    def test_natural_language_search_ignores_filler(self):
        rows = [("omarchy theme set", "Switch themes"),
                ("omarchy network", "Manage networks"),
                ("omarchy toggle nightlight", "Screen filter")]
        self.assertEqual(cap.rank_rows("can you show me wifi", rows)[0][0], "omarchy network")
        self.assertEqual(cap.rank_rows("turn off the night light", rows)[0][0], "omarchy toggle nightlight")

    def test_exact_route_beats_incidental_summary(self):
        rows = [("omarchy x", "theme set"), ("omarchy theme set <name>", "Switch theme")]
        self.assertEqual(cap.rank_rows("theme set", rows)[0][0], rows[1][0])

    def test_details_only_reads_metadata_for_exact_route(self):
        entry = {"route": "omarchy plugin clone", "args": "<id>",
                 "examples": ["omarchy plugin clone omarchy.clock"]}
        with mock.patch.object(cap, "command_catalogue", return_value=[entry]), \
             mock.patch.object(cap, "_run") as run:
            ok, text = d.lookup("command_details", "plugin clone")
            self.assertTrue(ok)
            self.assertIn("omarchy.clock", text)
            for query in ("plugin clone x", "plugin clone; touch /tmp/unwanted", "$(whoami)"):
                self.assertFalse(d.lookup("command_details", query)[0])
            run.assert_not_called()

    def test_pagination_reaches_whole_inventory(self):
        rows = [(f"app-{i}", "An application") for i in range(35)]
        with mock.patch.object(d, "applications", return_value=rows):
            ok, text = d.lookup("applications", "", limit=30)
            self.assertTrue(ok)
            self.assertIn("offset=30", text)
            self.assertNotIn("app-34", text)
            self.assertIn("app-34", d.lookup("applications", "", offset=30)[1])

    def test_invalid_options_return_errors(self):
        for kw in ({"limit": 0}, {"limit": 31}, {"limit": "3"}, {"offset": -1}, {"query": None}):
            self.assertFalse(d.lookup("applications", **kw)[0])
        self.assertFalse(d.lookup("not-a-topic")[0])

    def test_shortcuts_use_resolved_list_and_preserve_duplicate_chords(self):
        with mock.patch.object(cap, "_run", return_value="SUPER + V → Custom voice\nSUPER + V → Another action") as run:
            ok, text = d.lookup("shortcuts", "voice")
            self.assertTrue(ok)
            self.assertIn("Custom voice", text)
            self.assertIn("2 entries", text)
            run.assert_called_once_with(["omarchy", "menu", "keybindings", "--print"], timeout=20)

    def test_unavailable_shortcuts_do_not_become_stock_bindings(self):
        with mock.patch.object(cap, "_run", return_value="Couldn't connect"):
            ok, text = d.lookup("shortcuts")
            self.assertFalse(ok)
            self.assertIn("unavailable", text)

    def test_dispatcher_verification_checks_exact_namespace(self):
        stub = self.write("hl.lua", "---@class HL.DspWindowNamespace\n---@field move fun(...): HL.Dispatcher\nlocal x = {}\n")
        with mock.patch.object(cap, "HL_STUB", stub), \
             mock.patch.object(cap, "OMARCHY_PATH", self.root), \
             mock.patch.object(cap, "HYPR_ESSENTIALS", [
                 ("valid", "hl.dsp.window.move({})"),
                 ("invalid", "hl.dsp.workspace.move({})")]):
            self.assertEqual(len(cap.verify_hypr_essentials()), 1)
            self.assertIn("invalid", cap.verify_hypr_essentials()[0])
            self.assertIn("do not guess", d.dispatchers()[0][1])

    def test_dispatcher_examples_are_from_packaged_sources(self):
        stub = self.write("hl.lua", "---@class HL.DspWindowNamespace\n---@field float fun(...): HL.Dispatcher\nlocal x = {}\n")
        self.write("default/hypr/bindings/tiling.lua", 'o.bind("SUPER + T", "Float window", hl.dsp.window.float({ action = "toggle" }))\n')
        with mock.patch.object(cap, "HL_STUB", stub), mock.patch.object(cap, "OMARCHY_PATH", self.root):
            self.assertIn('hl.dsp.window.float({ action = "toggle" })', d.dispatchers()[0][1])

    def test_hidden_user_entry_masks_system_app_for_discovery_and_launch(self):
        self.write("user/test.desktop", "[Desktop Entry]\nHidden=true\n")
        self.write("system/test.desktop", "[Desktop Entry]\nType=Application\nName=Test\nExec=test\n")
        with mock.patch.object(d, "application_dirs", return_value=(self.root / "user", self.root / "system")):
            self.assertEqual(d.applications(), [])
            self.assertFalse(_desktop_entry_exists("test"))

    def test_nested_desktop_id_and_actions_use_same_launcher_resolution(self):
        self.write("apps/vendor/test.desktop", "[Desktop Entry]\nType=Application\nName=Editor\nExec=editor %F\nActions=new;missing;\n[Desktop Action new]\nName=New window\nExec=editor --new\n")
        with mock.patch.object(d, "application_dirs", return_value=(self.root / "apps",)):
            rows = d.applications()
            self.assertEqual(rows[0][0], "vendor-test")
            self.assertIn("vendor-test:new (New window)", rows[0][1])
            self.assertNotIn("missing", rows[0][1])
            self.assertTrue(_desktop_entry_exists("vendor-test"))
            self.assertEqual(desktop_actions("vendor-test"), ["new"])

    def test_desktop_action_name_does_not_replace_main_name(self):
        self.write("apps/test.desktop", "[Desktop Action new]\nName=Action name\n[Desktop Entry]\nType=Application\nName=Actual app\nExec=app\n")
        with mock.patch.object(d, "application_dirs", return_value=(self.root / "apps",)):
            self.assertEqual(d.applications(), [("test", "Actual app")])

    def test_xdg_search_roots_follow_environment(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/data", "XDG_DATA_DIRS": "/tmp/one:/tmp/two"}):
            self.assertEqual(d.application_dirs(), (Path("/tmp/data/applications"), Path("/tmp/one/applications"), Path("/tmp/two/applications")))

    def test_bad_and_invisible_desktop_files_are_skipped(self):
        self.write("apps/bad.desktop", "not an ini file")
        self.write("apps/invisible.desktop", "[Desktop Entry]\nType=Application\nName=Invisible\nNoDisplay=true\n")
        self.write("apps/unavailable.desktop", "[Desktop Entry]\nType=Application\nName=Missing\nTryExec=nonexistent-oma-test-binary\n")
        with mock.patch.object(d, "application_dirs", return_value=(self.root / "apps",)):
            self.assertEqual(d.applications(), [])

    def test_plugin_inventory_includes_bar_widget_manifests(self):
        self.write("shell/plugins/bar/Workspaces.manifest.json", json.dumps({"id": "omarchy.workspaces", "kinds": ["bar-widget"]}))
        self.write("user/omarchy/plugins/custom/manifest.json", json.dumps({"id": "user.clock"}))
        self.write("shell/plugins/broken/manifest.json", "invalid")
        with mock.patch.object(cap, "OMARCHY_PATH", self.root), mock.patch.object(d, "config_root", return_value=self.root / "user"), mock.patch.object(cap, "_run", return_value=""):
            rows = dict(d.plugins())
            self.assertEqual(set(rows), {"omarchy.workspaces", "user.clock"})
            self.assertIn("packaged", rows["omarchy.workspaces"])
            self.assertIn("state not inferred", rows["user.clock"])

    def test_plugin_runtime_state_is_distinguished_from_disk_inventory(self):
        self.write("shell/plugins/clock/manifest.json", json.dumps({"id": "omarchy.clock"}))
        state = [{"id": "omarchy.clock", "enabled": True, "active": False},
                 {"id": "runtime.only", "enabled": False, "active": False}]
        with mock.patch.object(cap, "OMARCHY_PATH", self.root), mock.patch.object(d, "config_root", return_value=self.root / "user"), mock.patch.object(cap, "_run", return_value=json.dumps(state)):
            rows = dict(d.plugins())
            self.assertIn("enabled=True, active=False", rows["omarchy.clock"])
            self.assertIn("manifest not located", rows["runtime.only"])

    def test_configuration_lists_absent_paths_without_reading_contents(self):
        self.write("hypr/input.lua", "SECRET MUST NOT APPEAR")
        with mock.patch.object(d, "config_root", return_value=self.root):
            text = d.lookup("configuration", "input")[1]
            self.assertIn("exists", text)
            self.assertNotIn("SECRET", text)
            self.assertIn("not present", d.lookup("configuration", "night light")[1])

    def test_hooks_show_names_without_executing_or_reading_scripts(self):
        self.write("omarchy/hooks/theme-set.d/example", "SECRET MUST NOT APPEAR")
        self.write("omarchy/hooks/theme-set.d/example.sample", "SECRET MUST NOT APPEAR")
        self.write("omarchy/hooks/post-boot", "SECRET MUST NOT APPEAR")
        with mock.patch.object(d, "config_root", return_value=self.root):
            rows = dict(d.hooks())
            self.assertIn("example", rows["theme-set"])
            self.assertIn("sample; not run", rows["theme-set"])
            self.assertIn("legacy", rows["post-boot"])
            self.assertNotIn("SECRET", str(rows))

    def test_help_runs_read_only_in_dry_run(self):
        config = Config()
        config.dry_run = True
        with mock.patch.object(d, "lookup", return_value=(True, "found it")) as lookup:
            result = Executor(config).call("omarchy_help", {"query": "clock", "topic": "plugins"})
            self.assertTrue(result.ok)
            self.assertEqual(result.output, "found it")
            lookup.assert_called_once_with("plugins", "clock", 12, 0)


if __name__ == "__main__":
    unittest.main()
