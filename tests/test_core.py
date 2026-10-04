#!/usr/bin/env python3
"""Offline tests for grabbit_core: .grab v2 round trip, v1 compatibility, input
validation, bundle build/read/extract, planning, and file restore into a scratch
$HOME. Nothing here installs packages or needs root/network.

    python3 tests/test_core.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import grabbit_core as core  # noqa: E402


def sample():
    return core.Manifest(
        header={"ORIG_DISTRO": "endeavouros", "ORIG_FAMILY": "arch", "ORIG_PM": "pacman",
                "ORIG_HOME": "/home/olduser"},
        packages=[core.Package("firefox", "pacman", "user"), core.Package("paru", "pacman", "distro"),
                  core.Package("grub", "pacman", "system"), core.Package("kate", "pacman", "de"),
                  core.Package("brave-bin", "aur", "user"), core.Package("org.gimp.GIMP", "flatpak", "user")],
        services=[core.Service("docker.service", "system", "docker")],
        groups=["docker"],
        files=[core.LooseFile("file", 0o755, 5, "local_bin_tool", "~/.local/bin/tool"),
               core.LooseFile("link", 0o777, 0, "", "~/.local/bin/alias", "~/.local/bin/tool")])


class ManifestTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        m = core.loads(core.dumps(sample()))
        self.assertEqual([p.key for p in m.packages], [p.key for p in sample().packages])
        self.assertEqual({p.name: p.category for p in m.packages}["kate"], "de")
        # system and old-distro packages start unticked
        self.assertEqual({p.name: p.selected for p in m.packages},
                         {"firefox": True, "paru": False, "grub": False, "kate": True,
                          "brave-bin": True, "org.gimp.GIMP": True})
        self.assertEqual(m.services[0].unit, "docker.service")
        self.assertEqual(m.groups, ["docker"])
        self.assertEqual(m.files[1].target, "~/.local/bin/tool")
        self.assertEqual(m.header["ORIG_HOME"], "/home/olduser")

    def test_v1_file_still_reads(self):
        m = core.loads((ROOT / "examples/cross-distro.grab").read_text())
        self.assertEqual(len(m.packages), 3)
        self.assertTrue(all(p.category == "user" for p in m.packages))

    def test_hostile_names_are_dropped(self):
        text = "PKG_LIST_START\nPKG ok-name:pacman\nPKG evil;rm -rf ~:pacman\nPKG $(id):aur\nPKG_LIST_END\n"
        self.assertEqual([p.name for p in core.loads(text).packages], ["ok-name"])

    def test_v1_reader_stops_before_v2_sections(self):
        # the bash CLI (v1 reader) must see exactly the PKG lines
        with tempfile.NamedTemporaryFile("w", suffix=".grab", delete=False) as f:
            f.write(core.dumps(sample()))
        out = subprocess.run(["awk", "/PKG_LIST_START/{f=1;next} /PKG_LIST_END/{exit} f && /^PKG /{c++} END{print c+0}",
                              f.name], capture_output=True, text=True).stdout.strip()
        os.unlink(f.name)
        self.assertEqual(out, "6")


class PlanTests(unittest.TestCase):
    def test_arch_plan_batches_and_orders(self):
        m = sample()
        for p in m.packages:
            p.selected, p.via = True, {"pacman": "repo", "aur": "aur", "flatpak": "flatpak"}[p.src]
        steps = core.plan(m, "arch", "pacman", bundle_dir="/x")
        labels = [s.label for s in steps]
        self.assertTrue(labels[0].startswith("Install 4 repo packages"))
        self.assertEqual(steps[0].argv[:4], ["pacman", "-Syu", "--needed", "--noconfirm"])
        self.assertTrue(steps[0].root)
        paru = next(s for s in steps if s.label.startswith("Install 1 AUR"))
        self.assertFalse(paru.root)                      # paru must not run as root
        self.assertIn("--sudoflags", paru.argv)
        self.assertLess(labels.index("Make sure paru (AUR helper) is installed"), labels.index(paru.label))
        self.assertTrue(any(s.argv[0] == "restore-files" for s in steps))
        self.assertTrue(labels[-1].startswith("Add "))   # groups last, after packages create them

    def test_service_follows_its_package(self):
        m = sample()
        m.packages.append(core.Package("docker", "pacman", "user", False, "repo"))
        for p in m.packages[:-1]:
            p.selected, p.via = False, "repo"
        steps = core.plan(m, "arch", "pacman", update_first=False)
        self.assertFalse(any("services" in s.label for s in steps))
        m.packages[-1].selected = True
        steps = core.plan(m, "arch", "pacman", update_first=False)
        self.assertTrue(any(s.label == "Enable 1 system services" for s in steps))

    def test_unavailable_is_skipped(self):
        m = sample()
        for p in m.packages:
            p.selected, p.via = True, "unavailable"
        steps = core.plan(m, "arch", "pacman", update_first=False)
        self.assertFalse(any("repo packages" in s.label for s in steps))


FAKE_PACMAN = r'''#!/usr/bin/env python3
import sys
installed = {"ufw", "jack2", "code", "eza", "python-fastapi", "neovim"}
provided = installed | {"nvim-wrapper"}          # neovim provides nvim-wrapper
sync = {"firewalld": [], "vim": [], "ufw": [], "pipewire-jack": ["jack2"], "nvim-wrapper": []}
a = sys.argv[1:]
if a == ["-Qq"]: print("\n".join(sorted(installed)))
elif a == ["-Slq"]: print("\n".join(sync))
elif a[0] == "-T": print("\n".join(x for x in a[1:] if x not in provided)); sys.exit(127 if any(x not in provided for x in a[1:]) else 0)
elif a[0] == "-Si":
    for n in a[1:]:
        if n in sync:
            print(f"Repository      : extra\nName            : {n}\nProvides        : None\n"
                  f"Conflicts With  : {' '.join(sync[n]) or 'None'}\n")
'''


class TargetConflictTests(unittest.TestCase):
    def test_already_there_conflicting_and_same_role_start_unticked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "pacman"
            fake.write_text(FAKE_PACMAN)
            fake.chmod(0o755)
            old_path, old_aur = os.environ["PATH"], core.aur_lookup
            os.environ["PATH"] = f"{tmp}:{old_path}"
            core.aur_lookup = lambda names, log=print: {"visual-studio-code-bin": {"Conflicts": ["code"]}}
            try:
                m = core.Manifest(packages=[
                    core.Package("firewalld", "pacman"), core.Package("vim", "pacman"),
                    core.Package("ufw", "pacman"), core.Package("pipewire-jack", "pacman"),
                    core.Package("visual-studio-code-bin", "aur"), core.Package("nvim-wrapper", "pacman"),
                    core.Package("eza", "brew"), core.Package("fastapi", "pip"), core.Package("rare", "pip"),
                    core.Package("vim", "brew")])
                core.resolve(m, "arch", "pacman", log=lambda _: None)
            finally:
                os.environ["PATH"], core.aur_lookup = old_path, old_aur
            got = {p.key: (p.selected, p.note) for p in m.packages}
            self.assertEqual(got["firewalld:pacman"], (False, "this system's firewall is ufw"))
            self.assertEqual(got["vim:pacman"], (True, ""))
            self.assertEqual(got["ufw:pacman"], (False, "already installed"))
            self.assertEqual(got["pipewire-jack:pacman"], (False, "conflicts with installed jack2"))
            self.assertEqual(got["visual-studio-code-bin:aur"], (False, "conflicts with installed code"))
            self.assertEqual(got["nvim-wrapper:pacman"], (False, "already provided by an installed package"))
            self.assertEqual(got["eza:brew"], (False, "already installed by pacman"))
            self.assertEqual(got["fastapi:pip"], (False, "already installed by pacman"))
            self.assertEqual(got["rare:pip"], (True, ""))
            self.assertEqual(got["vim:brew"], (False, "the bundle installs vim natively"))

    def test_unticked_package_takes_its_service_along(self):
        m = core.Manifest(packages=[core.Package("firewalld", "pacman", "user", False, "repo")],
                          services=[core.Service("firewalld.service", "system", "firewalld")])
        steps = core.plan(m, "arch", "pacman", update_first=False)
        self.assertFalse(any("firewalld.service" in s.argv for s in steps))


class BundleTests(unittest.TestCase):
    def test_build_read_extract_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src_home = tmp / "src"
            (src_home / ".local/bin").mkdir(parents=True)
            tool = src_home / ".local/bin/tool"
            tool.write_text("#!/bin/sh\necho hi\n")
            tool.chmod(0o755)
            m = sample()
            m.files = [core.LooseFile("file", 0o755, tool.stat().st_size, "local_bin_tool", str(tool)),
                       core.LooseFile("link", 0o777, 0, "", "~/.local/bin/alias", "~/.local/bin/tool")]
            out = core.build_bundle(m, tmp / "b.grab.run", log=lambda _: None)
            self.assertTrue(os.access(out, os.X_OK))
            self.assertTrue(out.read_bytes().startswith(b"#!/usr/bin/env bash"))
            self.assertEqual(len(core.load_path(out).packages), 6)

            d = core.extract_bundle(out, tmp / "unpacked")
            self.assertTrue((d / "app/grabbit_core.py").is_file())
            self.assertTrue((d / "files/local_bin_tool").is_file())

            # restore into a scratch $HOME (file restored at its absolute path here,
            # the link under the new home)
            new_home = tmp / "newhome"
            new_home.mkdir()
            env = dict(os.environ, HOME=str(new_home))
            code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import grabbit_core as c;"
                    f"m=c.load_path({str(d / 'manifest.grab')!r});"
                    "m.files[0].dest='~/.local/bin/tool';"
                    f"r=c.Runner([c.Step('r',['restore-files'],items=m.files)], c.Sudo(), bundle_dir={str(d)!r},"
                    "log=lambda _: None); r._run(); print(r.results[0][1])")
            res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            self.assertEqual(res.stdout.strip(), "True", res.stderr)
            self.assertEqual((new_home / ".local/bin/tool").read_text(), "#!/bin/sh\necho hi\n")
            self.assertTrue(os.access(new_home / ".local/bin/tool", os.X_OK))
            self.assertEqual(os.readlink(new_home / ".local/bin/alias"), str(new_home / ".local/bin/tool"))

    def test_accounts_move_to_a_new_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old, new = tmp / "home/olduser", tmp / "home/newuser"
            (old / ".claude/projects" / core.claude_project_name(old) / "memory").mkdir(parents=True)
            (old / ".claude/projects" / core.claude_project_name(old) / "memory/MEMORY.md").write_text(
                f"- tool at {old}/tool\n")
            (old / ".claude/.credentials.json").write_text('{"token": "x"}')
            (old / ".claude/.credentials.json").chmod(0o600)
            (old / ".claude.json").write_text(f'{{"projects": {{"{old}": {{}}}}}}')
            (old / ".gitconfig").write_text("[user]\n\tname = me\n")
            (old / ".ssh").mkdir(mode=0o700)
            (old / ".ssh/id_ed25519").write_text("KEY")
            new.mkdir(parents=True)
            saved = (core.HOME, core.gh_token)
            try:
                core.HOME, core.gh_token = old, lambda: "gho_test"
                acct = core.capture_accounts()
                dests = {f.dest for f in acct}
                self.assertTrue({"~/.gitconfig", "~/.ssh", "~/.claude.json", "~/.claude/.credentials.json",
                                 "~/.claude/projects", core.GH_TOKEN_DEST} <= dests)
                self.assertTrue(all(f.key.startswith(core.ACCOUNT_KEY) for f in acct))
                m = sample()
                m.header["ORIG_HOME"] = str(old)
                m.files = acct
                out = core.build_bundle(m, tmp / "b.grab.run", log=lambda _: None)
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)   # holds sign-ins
                d = core.extract_bundle(out, tmp / "unpacked")
                self.assertEqual((d / f"files/{core.ACCOUNT_KEY}gh-token").read_text(), "gho_test\n")

                core.HOME = new
                steps = core.plan(core.load_path(d / "manifest.grab"), "arch", "pacman", bundle_dir=str(d),
                                  update_first=False)
                self.assertIn("gh-login", [s.argv[0] for s in steps])
                files = next(s for s in steps if s.argv[0] == "restore-files")
                r = core.Runner([files], core.Sudo(), bundle_dir=str(d), log=lambda _: None)
                r._run()
                self.assertTrue(r.results[0][1], r.results)
            finally:
                core.HOME, core.gh_token = saved
            proj = new / ".claude/projects" / core.claude_project_name(new)
            self.assertEqual((proj / "memory/MEMORY.md").read_text(), f"- tool at {new}/tool\n")
            self.assertFalse((new / ".claude/projects" / core.claude_project_name(old)).exists())
            self.assertIn(str(new), (new / ".claude.json").read_text())
            self.assertNotIn(str(old), (new / ".claude.json").read_text())
            self.assertEqual((new / ".claude/.credentials.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((new / ".ssh").stat().st_mode & 0o777, 0o700)
            self.assertEqual((new / ".config/grabbit/gh-token").read_text(), "gho_test\n")

    def test_stub_ends_with_marker(self):
        self.assertTrue((ROOT / "grabbit-bundle-stub.sh").read_text().rstrip().endswith(core.PAYLOAD_MARKER))


if __name__ == "__main__":
    unittest.main(verbosity=2)
