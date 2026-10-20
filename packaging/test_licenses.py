"""Focused regression tests for redistribution notices (stdlib only)."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile


HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


licensing = module("license_collector", HERE / "licenses.py")
package = module("localai_packaging", HERE / "package.py")


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.collector = licensing.Collector(self.stage, self.root / "overrides")

    def file(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_python_collects_original_and_declared_nested_license(self):
        dist = self.root / "site" / "example-1.0.dist-info"
        self.file("site/example-1.0.dist-info/METADATA",
                  "Name: Example\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: nested/original.txt\n\n")
        self.file("site/example-1.0.dist-info/licenses/nested/original.txt", "Copyright Example\nMIT original")
        self.collector.python_packages(dist.parent)
        record = self.collector.entries[0]
        copied = self.stage / record["files"][0]["path"]
        self.assertEqual(copied.read_text(), "Copyright Example\nMIT original")
        self.assertEqual(record["declaredLicense"], "MIT")

    def test_relative_build_paths_and_absolute_metadata_paths(self):
        self.file("site/example-1.0.dist-info/METADATA",
                  "Name: Example\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: LICENSE\n\n")
        self.file("site/example-1.0.dist-info/licenses/LICENSE", "Original copyright")
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.collector.python_packages(Path("site"))
        finally:
            os.chdir(previous)
        self.assertEqual(len(self.collector.entries), 1)

    def test_missing_declared_text_blocks_collection(self):
        self.file("site/example-1.0.dist-info/METADATA",
                  "Name: Example\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: LICENSE\n\n")
        with self.assertRaises(licensing.LicenseError):
            self.collector.python_packages(self.root / "site")

    def test_generic_expression_is_not_a_substitute_for_original_notice(self):
        source = self.root / "package"
        source.mkdir()
        with self.assertRaises(licensing.LicenseError):
            self.collector.add("nuget", "Example", "1.0", "MIT", source, [])

    def test_nuget_originals_and_native_notices(self):
        self.file("cache/example/1.0/example.nuspec",
                  '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata>'
                  '<license type="expression">MIT</license></metadata></package>')
        self.file("cache/example/1.0/LICENSE.txt", "Example original copyright")
        self.file("cache/example/1.0/native/THIRD-PARTY-NOTICES.txt", "Native component notice")
        self.collector.nuget_package("Example", "1.0", [self.root / "cache"])
        self.assertEqual(len(self.collector.entries[0]["files"]), 2)

    def test_runtime_graph_excludes_unshipped_build_tools(self):
        cache = self.root / "cache"
        for name in ("deployed", "microsoft.netcore.app.runtime.linux-x64"):
            self.file(f"cache/{name}/1.0/{name}.nuspec",
                      '<package><metadata><license type="expression">MIT</license></metadata></package>')
            self.file(f"cache/{name}/1.0/LICENSE", f"Original notice for {name}")
        assets = self.file("project.assets.json", json.dumps({
            "packageFolders": {str(cache): {}},
            "libraries": {"deployed/1.0": {"type": "package", "path": "deployed/1.0"},
                          "build-only/1.0": {"type": "package", "path": "build-only/1.0"}}}))
        self.file("stage/app/App.deps.json", json.dumps({"libraries": {
            "deployed/1.0": {"type": "package"},
            "runtimepack.Microsoft.NETCore.App.Runtime.linux-x64/1.0": {"type": "runtimepack"}}}))
        self.collector.nuget([assets])
        self.assertEqual({e["name"] for e in self.collector.entries},
                         {"deployed", "microsoft.netcore.app.runtime.linux-x64"})

    def test_version_specific_reviewed_override(self):
        source = self.root / "empty"
        source.mkdir()
        self.file("overrides/nuget/example/1.0/review.json", json.dumps({
            "license": "MIT", "source": "https://example.com/releases/1.0/LICENSE",
            "reason": "Original release license verified"}))
        self.file("overrides/nuget/example/1.0/LICENSE", "Original release copyright")
        self.collector.add("nuget", "Example", "1.0", "MIT", source, [])
        self.assertIn("review", self.collector.entries[0])

    def test_shared_registry_is_version_bound_and_preserves_font_license(self):
        source = self.root / "empty"
        source.mkdir()
        self.file("upstream/LICENSE", "Original copyright")
        self.file("upstream/OFL.txt", "Original font license")
        self.file("overrides.json", json.dumps({"schemaVersion": 1, "packages": {
            "nuget/example/1.0": {"license": "MIT AND OFL-1.1",
                                  "source": "https://example.com/releases/1.0/LICENSE",
                                  "reason": "Reviewed source release",
                                  "files": ["upstream/LICENSE", "upstream/OFL.txt"]}}}))
        self.collector.add("nuget", "example", "1.0", "MIT", source, [])
        self.assertEqual(len(self.collector.entries[0]["files"]), 2)
        self.assertEqual(self.collector.entries[0]["declaredLicense"], "MIT AND OFL-1.1")
        with self.assertRaises(licensing.LicenseError):
            self.collector.add("nuget", "example", "1.1", "MIT", source, [])

    def test_paths_cannot_escape_package(self):
        self.file("outside.txt", "not a package license")
        directory = self.root / "package"
        directory.mkdir()
        with self.assertRaises(licensing.LicenseError):
            licensing.child(directory, "../outside.txt")

    def test_tampered_notice_fails_verification(self):
        for filename in ("LICENSE", "THIRD-PARTY-NOTICES.md", "licenses/Inter-OFL-1.1.txt"):
            self.file("stage/" + filename, "original")
        for ecosystem in ("python", "nuget", "runtime"):
            source = self.root / ecosystem
            source.mkdir()
            text = self.file(ecosystem + "/LICENSE", "original copyright")
            self.collector.add(ecosystem, "example", "1.0", "MIT", source, [text])
        self.collector.write()
        licensing.verify(self.stage)
        evidence = self.stage / self.collector.entries[0]["files"][0]["path"]
        evidence.write_text("changed")
        with self.assertRaises(licensing.LicenseError):
            licensing.verify(self.stage)

    def test_empty_inventory_fails_verification(self):
        for filename in ("LICENSE", "THIRD-PARTY-NOTICES.md", "licenses/Inter-OFL-1.1.txt"):
            self.file("stage/" + filename, "original")
        self.collector.write()
        with self.assertRaises(licensing.LicenseError):
            licensing.verify(self.stage)

    def test_real_archives_include_project_and_font_licenses(self):
        for command in (package.config_zip, package.docs_zip):
            output = self.root / (command.__name__ + ".zip")
            command(argparse.Namespace(out=output, version="test"))
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                for suffix in ("/LICENSE", "/THIRD-PARTY-NOTICES.md",
                               "/licenses/Inter-OFL-1.1.txt", "/licenses/RELEASE-CHECKLIST.md"):
                    name = next(n for n in names if n.endswith(suffix))
                    self.assertTrue(archive.read(name))


if __name__ == "__main__":
    unittest.main()
