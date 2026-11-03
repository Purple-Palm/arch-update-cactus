#!/usr/bin/env python3
"""The fixes catalogue (lib/fixes): what each fix plans for a result, and its root shell run for real against
stand-in commands (mkinitcpio, lsinitcpio, depmod, pacman, snapper… in a folder first on PATH) on files in a
temporary folder: a fix that works, one whose command fails, one whose result is wrong, and every time the files
as they were afterwards, checked byte for byte. Nothing here needs root or touches the machine."""

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
os.environ["ARCH_UPDATE_REPORT_URL"] = ""          # never the real relay

import engine  # noqa: E402
import fixes  # noqa: E402

KVER = "7.2.14-arch1-1"


def listing(kver=KVER, modules=True):
    """What lsinitcpio lists for an image (the stand-in images hold their own listing)."""
    lines = [f"usr/lib/modules/{kver}", f"usr/lib/modules/{kver}/kernel"]
    if modules:
        lines += [f"usr/lib/modules/{kver}/kernel/ext4.ko.zst", f"usr/lib/modules/{kver}/modules.dep.bin"]
    return "\n".join(["bin", "init", "usr/lib/modules"] + lines) + "\n"


def write(path, text, mode=0o755):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Runner(unittest.TestCase):
    """The root shell, run as this user with stand-ins for the system's commands."""

    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="fixes-test-")
        self.bin = os.path.join(self.t, "bin")
        self.boot = os.path.join(self.t, "boot")
        os.makedirs(self.bin)
        os.makedirs(self.boot)
        self.image = os.path.join(self.boot, "initramfs-linux.img")
        self.kernel = os.path.join(self.boot, "vmlinuz-linux")
        write(self.image, "OLD\n" + listing(), 0o600)
        write(self.kernel, "KERNEL-OLD\n", 0o644)
        self.result = os.path.join(self.t, "result")
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", T=self.t, IMAGES=self.image)
        # stand-ins: what they do is set by files in $T (mode-*) and by $IMAGES
        self.stub("lsinitcpio", 'case $(head -n 1 "$1") in BROKEN*) exit 1 ;; esac\ntail -n +2 "$1"\n')
        self.stub("mkinitcpio", r'''
echo "==> Building image from preset: /etc/mkinitcpio.d/$2.preset: 'default'"
mode=$(cat "$T/mode-mkinitcpio" 2>/dev/null || echo good)
for f in $IMAGES; do
    case $mode in
        good) printf 'NEW\n%s' "$(cat "$T/good-listing")" >"$f" ;;
        nomodules) printf 'NEW\n%s' "$(cat "$T/bare-listing")" >"$f"; echo "==> WARNING: No modules were added to the image. This is probably not what you want." ;;
        truncated) : >"$f"; echo "==> ERROR: errors were encountered during the build. The image may not be complete."; exit 1 ;;
        slow) sleep 2; printf 'NEW\n%s' "$(cat "$T/good-listing")" >"$f" ;;
    esac
done
echo "==> Initcpio image generation successful"
''')
        write(os.path.join(self.t, "good-listing"), listing(), 0o644)
        write(os.path.join(self.t, "bare-listing"), listing(modules=False), 0o644)
        self.stub("pgrep", "exit 1\n")              # no pacman running

    def tearDown(self):
        for root, dirs, _ in os.walk(self.t):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        shutil.rmtree(self.t, ignore_errors=True)

    def stub(self, name, body):
        write(os.path.join(self.bin, name), "#!/bin/bash\n" + body)

    def run_fixes(self, *fixes_args, stdin="", timeout=60):
        """The runner with these fixes: [(key, [args])]. Returns (exit status, output, results)."""
        argv = ["bash", "-c", fixes.runner(), "arch-update-fix", self.result]
        for key, args in fixes_args:
            argv += [key, *args, "--"]
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=self.env, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, fixes.read_results(self.result)

    def image_spec(self, path=None, kver=KVER):
        return f"i:{kver}:1:{path or self.image}"

    def leftovers(self):
        return [n for n in os.listdir("/tmp") if n.startswith("arch-update-fix.")
                and os.stat(os.path.join("/tmp", n)).st_uid == os.getuid()
                and os.stat(os.path.join("/tmp", n)).st_mtime >= self.started]

    def run(self, result=None):
        self.started = time.time() - 1
        return super().run(result)

    # ── initramfs ──

    def test_initramfs_rebuilt_and_checked(self):
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(results, [("initramfs", "ok")])
        self.assertTrue(read(self.image).startswith("NEW"))
        self.assertIn("собран под ядро", out)
        self.assertEqual(self.leftovers(), [])

    def test_an_image_without_modules_is_put_back(self):
        """The Chromebook: mkinitcpio warns "No modules were added" and exits 0; the old image comes back."""
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "nomodules", 0o644)
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(results, [("initramfs", "rolledback")])
        self.assertEqual(digest(self.image), before)
        self.assertEqual(stat.S_IMODE(os.stat(self.image).st_mode), 0o600)
        self.assertIn("сверил с копиями", out)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.image + ".arch-update-new"))

    def test_a_failed_build_is_put_back(self):
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "truncated", 0o644)
        status, out, _ = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(digest(self.image), before)

    def test_an_image_for_another_kernel_is_put_back(self):
        before = digest(self.image)
        write(os.path.join(self.t, "good-listing"), listing("7.1.0-arch1-1"), 0o644)
        status, out, _ = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertIn("собран не под ядро", out)
        self.assertEqual(digest(self.image), before)

    def test_an_image_that_was_missing_is_removed_again(self):
        """A missing image is remembered as missing: if the new one is not right, it does not stay."""
        os.remove(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "nomodules", 0o644)
        status, _, _ = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK)
        self.assertFalse(os.path.exists(self.image))

    def test_no_room_for_copies_changes_nothing(self):
        before = digest(self.image)
        self.stub("df", 'printf "Avail\\n1024\\n"\n')
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.NO_BACKUP, out)
        self.assertEqual(results, [("initramfs", "nobackup")])
        self.assertIn("ничего не менял", out)
        self.assertEqual(digest(self.image), before)

    def test_when_putting_back_fails_the_copies_stay_and_it_says_where(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        write(os.path.join(self.t, "mode-mkinitcpio"), "nomodules", 0o644)
        # mkinitcpio writes the new image, then the folder may not be written any more
        self.stub("mkinitcpio", f'printf "NEW\\n%s" "$(cat "$T/bare-listing")" >"{self.image}"\n'
                                f'chmod 0400 "{self.image}"; chmod 0555 "{self.boot}"\n'
                                'echo "No modules were added to the image"\n')
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLBACK_FAILED, out)
        self.assertEqual(results, [("initramfs", "rollbackfailed")])
        self.assertIn("копии остались в /tmp/arch-update-fix.", out)
        kept = [os.path.join("/tmp", n) for n in self.leftovers()]
        self.assertEqual(len(kept), 1)
        self.assertTrue(read(os.path.join(kept[0], "0")).startswith("OLD"))
        shutil.rmtree(kept[0])

    def test_ctrl_c_does_not_cut_a_fix_off(self):
        import pexpect
        write(os.path.join(self.t, "mode-mkinitcpio"), "slow", 0o644)
        argv = ["bash", "-c", fixes.runner(), "arch-update-fix", self.result, "initramfs", "p:linux",
                self.image_spec(), "--"]
        child = pexpect.spawn(argv[0], argv[1:], env=self.env, timeout=30)
        child.expect("mkinitcpio -p linux")
        time.sleep(0.3)
        child.sendintr()                        # Ctrl+C in that terminal
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, fixes.OK)
        self.assertTrue(read(self.image).startswith("NEW"))

    # ── depmod ──

    def depmod_setup(self):
        self.mods = os.path.join(self.t, "modules")
        os.makedirs(os.path.join(self.mods, KVER))
        self.ld = os.path.join(self.t, "ld.so.cache")
        write(self.ld, "LDCACHE-OLD", 0o644)
        return [f"m:{self.mods}", f"l:{self.ld}", KVER]

    def test_depmod_writes_the_module_list(self):
        args = self.depmod_setup()
        self.stub("depmod", f'echo index >"{self.mods}/$1/modules.dep.bin"; echo dep >"{self.mods}/$1/modules.dep"\n')
        self.stub("ldconfig", f'echo LDCACHE-NEW >"{self.ld}"\n')
        status, out, results = self.run_fixes(("depmod", args))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(results, [("depmod", "ok")])
        self.assertTrue(os.path.exists(os.path.join(self.mods, KVER, "modules.dep.bin")))

    def test_depmod_that_fails_leaves_nothing_half_written(self):
        """A full disk: depmod leaves a partial list and fails, ldconfig cuts its cache short."""
        args = self.depmod_setup()
        self.stub("depmod", f'echo partial >"{self.mods}/$1/modules.dep"\n'
                            'echo "depmod: FATAL: could not rename: No space left on device" >&2\nexit 1\n')
        self.stub("ldconfig", f': >"{self.ld}"\nexit 1\n')
        status, out, _ = self.run_fixes(("depmod", args))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertFalse(os.path.exists(os.path.join(self.mods, KVER, "modules.dep")))
        self.assertEqual(read(self.ld), "LDCACHE-OLD")

    def test_a_strange_kernel_version_is_refused(self):
        args = self.depmod_setup()
        status, out, _ = self.run_fixes(("depmod", args[:2] + ["../../etc"]))
        self.assertEqual(status, fixes.NO_BACKUP, out)

    def test_depmod_then_initramfs_and_a_failure_stops_the_rest(self):
        args = self.depmod_setup()
        self.stub("depmod", "exit 1\n")
        self.stub("ldconfig", "exit 0\n")
        status, _, results = self.run_fixes(("depmod", args), ("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK)
        self.assertEqual(results, [("depmod", "rolledback"), ("initramfs", "skipped")])
        self.assertTrue(read(self.image).startswith("OLD"))

    # ── kernel ──

    def test_the_kernel_again_from_the_cache(self):
        pkg = os.path.join(self.t, "linux-7.2.14.arch1-1-x86_64.pkg.tar.zst")
        write(pkg, "pkg", 0o644)
        self.stub("pacman", f'echo "$@" >"$T/pacman-args"\necho KERNEL-NEW >"{self.kernel}"\n'
                            f'printf "NEW\\n%s" "$(cat "$T/good-listing")" >"{self.image}"\n')
        status, out, _ = self.run_fixes(("kernel", [f"c:{pkg}", f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(read(os.path.join(self.t, "pacman-args")).split(), ["-U", pkg])

    def test_a_kernel_reinstall_said_no_to_changes_nothing(self):
        before = digest(self.kernel), digest(self.image)
        self.stub("pacman", f': >"{self.kernel}"\necho ":: Proceed with installation? [Y/n] n"\nexit 1\n')
        status, _, _ = self.run_fixes(("kernel", ["r:linux", f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK)
        self.assertEqual((digest(self.kernel), digest(self.image)), before)

    # ── dkms, sbctl, mount ──

    def test_dkms_built_or_not(self):
        self.stub("dkms", 'case $1 in autoinstall) exit 0 ;; status) cat "$T/dkms-status" ;; esac\n')
        write(os.path.join(self.t, "dkms-status"), f"nvidia/615.78, {KVER}, x86_64: installed\n", 0o644)
        self.assertEqual(self.run_fixes(("dkms", [f"nvidia:{KVER}"]))[0], fixes.OK)
        write(os.path.join(self.t, "dkms-status"), "nvidia/615.78: added\n", 0o644)
        status, out, _ = self.run_fixes(("dkms", [f"nvidia:{KVER}"]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertIn("так и не собран", out)

    def test_sbctl_signs_or_puts_the_file_back(self):
        self.stub("sbctl", f'case $1 in sign) echo SIGNED >>"$3" ;; verify) echo "✓ $(cat "$T/signed") is signed" ;; esac\n')
        write(os.path.join(self.t, "signed"), self.kernel, 0o644)
        self.assertEqual(self.run_fixes(("sbctl", [self.kernel]))[0], fixes.OK)
        before = digest(self.kernel)
        write(os.path.join(self.t, "signed"), "/somewhere/else", 0o644)
        status, _, _ = self.run_fixes(("sbctl", [self.kernel]))
        self.assertEqual(status, fixes.ROLLED_BACK)
        self.assertEqual(digest(self.kernel), before)

    def test_mount_only_a_boot_partition(self):
        self.stub("mountpoint", 'test -e "$T/mounted"\n')
        self.stub("mount", 'echo "$@" >"$T/mount-args"; : >"$T/mounted"\n')
        self.assertEqual(self.run_fixes(("mount", ["/boot"]))[0], fixes.OK)
        self.assertEqual(read(os.path.join(self.t, "mount-args")).split(), ["--", "/boot"])
        os.remove(os.path.join(self.t, "mounted"))
        self.assertEqual(self.run_fixes(("mount", ["/home"]))[0], fixes.ROLLED_BACK)
        self.assertFalse(os.path.exists(os.path.join(self.t, "mounted")))

    # ── space ──

    def space_stubs(self, frees):
        """df answers these free sizes (bytes) one after the other, the last one from then on."""
        write(os.path.join(self.t, "frees"), "\n".join(str(f) for f in frees) + "\n", 0o644)
        self.stub("df", r'''
n=$(cat "$T/df-n" 2>/dev/null || echo 0); echo $((n + 1)) >"$T/df-n"
v=$(sed -n "$((n + 1))p" "$T/frees"); [ -n "$v" ] || v=$(tail -n 1 "$T/frees")
case "$*" in *"$T"*|*/tmp*) v=99999999999 ;; esac
printf 'Avail\n%s\n' "$v"
''')
        self.stub("pacman", 'echo "$@" >>"$T/calls"\n')
        self.stub("journalctl", 'echo "journalctl $*" >>"$T/calls"\n')
        self.stub("snapper", r'''echo "snapper $*" >>"$T/calls"
case "$*" in
    *list-configs*) printf 'config\nroot\n' ;;
    *" list "*) echo "number,cleanup"; for i in $(seq 1 40); do echo "$i,number"; done ;;
esac
''')

    def test_space_freed_without_asking(self):
        self.space_stubs([10 ** 9, 3 * 10 ** 9])
        status, out, results = self.run_fixes(("space", [str(2 * 10 ** 9)]))
        self.assertEqual(status, fixes.OK, out)
        calls = read(os.path.join(self.t, "calls"))
        self.assertIn("-Sc --noconfirm", calls)
        self.assertIn("journalctl --vacuum-size=200M", calls)
        self.assertIn("snapper -c root cleanup number", calls)
        self.assertNotIn("set-config", calls)

    def test_snapshots_go_only_when_the_user_says_yes(self):
        self.space_stubs([10 ** 9])
        status, out, results = self.run_fixes(("space", [str(2 * 10 ** 9)]), stdin="\n")
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        self.assertEqual(results, [("space", "notenough")])
        self.assertIn("Оставить 10 последних снимков? [y/N]", out)
        self.assertNotIn("set-config", read(os.path.join(self.t, "calls")))
        os.remove(os.path.join(self.t, "calls"))
        os.remove(os.path.join(self.t, "df-n"))
        self.space_stubs([10 ** 9, 10 ** 9, 10 ** 9, 5 * 10 ** 9])
        status, out, _ = self.run_fixes(("space", [str(2 * 10 ** 9)]), stdin="y\n")
        self.assertEqual(status, fixes.OK, out)
        self.assertIn("set-config NUMBER_LIMIT=10 NUMBER_LIMIT_IMPORTANT=5", read(os.path.join(self.t, "calls")))

    # ── the driver ──

    def test_unknown_fix_and_pacman_at_work(self):
        status, out, results = self.run_fixes(("nosuchfix", []))
        self.assertEqual((status, results), (fixes.NO_BACKUP, [("nosuchfix", "unknown")]))
        self.stub("pgrep", "echo 123\n")
        before = digest(self.image)
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual((status, results), (fixes.REFUSED, [("initramfs", "refused")]))
        self.assertIn("работает pacman", out)
        self.assertEqual(digest(self.image), before)

    def test_the_runner_passes_shellcheck(self):
        if not shutil.which("shellcheck"):
            self.skipTest("no shellcheck")
        path = os.path.join(self.t, "runner.sh")
        write(path, "#!/bin/bash\n" + fixes.runner())
        # SC2329: the fixes' functions are called by name (${key}_apply)
        p = subprocess.run(["shellcheck", "-s", "bash", "-e", "SC2329", path], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)


class Plans(unittest.TestCase):
    """What each fix plans for a result, on a stand-in machine (no pacman, presets in a temporary folder)."""

    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="fixes-plan-")
        self.saved = {}
        self.preset_dir = os.path.join(self.t, "mkinitcpio.d")
        os.makedirs(self.preset_dir)
        write(os.path.join(self.preset_dir, "linux.preset"), "", 0o644)
        self.patch(engine, "PRESET_DIR", self.preset_dir)
        self.patch(engine, "kernel_packages", lambda: {"linux": KVER})
        self.patch(engine, "preset_files", lambda pkg: [("kernel", "/boot/vmlinuz-linux"),
                                                        ("image", "/boot/initramfs-linux.img")])
        self.patch(engine, "modular", lambda kver: True)
        self.patch(engine, "elevation_command", lambda: "sudo")
        self.patch(engine, "installed_versions", lambda: {"linux": "7.2.14.arch1-1"})
        self.patch(engine, "cached_package", lambda p, v: None)
        self.patch(engine, "run", lambda cmd, **k: "")

    def tearDown(self):
        for (obj, name), value in self.saved.items():
            setattr(obj, name, value)
        shutil.rmtree(self.t, ignore_errors=True)

    def patch(self, obj, name, value):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, value)

    def keys(self, res):
        return [f["key"] for f in fixes.plan(res)]

    def test_the_chromebook(self):
        """depmod failed on a full disk, mkinitcpio built an image without modules."""
        res = {"problems": [engine.DISK_FULL],
               "boot": [{"kind": "depmod", "kvers": [KVER], "text": "depmod"},
                        {"kind": "initcpio", "text": "в initramfs не попал ни один модуль"}]}
        plan = fixes.plan(res)
        self.assertEqual([f["key"] for f in plan], ["space", "depmod", "initramfs"])
        self.assertEqual(plan[0]["args"], [str(fixes.space.AFTER_FULL)])
        self.assertEqual(plan[1]["args"][-1], KVER)
        self.assertEqual(plan[2]["args"], ["p:linux", f"i:{KVER}:1:/boot/initramfs-linux.img"])
        argv = fixes.command(plan, "/home/u/.local/state/arch-update-auto/fix-result-x")
        self.assertEqual(argv[:3], ["sudo", "bash", "-c"])
        self.assertEqual(argv[4:6], ["arch-update-fix", "/home/u/.local/state/arch-update-auto/fix-result-x"])
        self.assertEqual(argv.count("--"), 3)

    def test_before_the_update_the_disk_was_too_small(self):
        plan = fixes.plan({"space": {"need": 3 * 10 ** 9, "free": 10 ** 9}})
        self.assertEqual([(f["key"], f["args"]) for f in plan], [("space", [str(3 * 10 ** 9)])])

    def test_nothing_for_a_clean_result_or_odd_input(self):
        self.assertEqual(fixes.plan({}), [])
        self.assertEqual(fixes.plan(None), [])
        self.assertEqual(fixes.plan({"boot": "x", "space": "y"}), [])
        self.assertEqual(self.keys({"boot": [{"kind": "unmounted", "target": "/home", "text": "t"},
                                             {"kind": "depmod", "kver": "../x", "text": "t"},
                                             {"kind": "unsigned", "files": ["relative", "/a/../b"], "text": "t"},
                                             {"kind": "flash", "text": "dd"}]}), [])
        self.assertEqual(fixes.command([{"key": "rm", "args": ["-rf", "/"]}]), [])

    def test_order_and_what_follows_what(self):
        res = {"boot": [{"kind": "unsigned", "files": ["/boot/vmlinuz-linux"], "text": "t"},
                        {"kind": "dkms", "module": "nvidia", "kvers": [KVER], "text": "t"},
                        {"kind": "unmounted", "target": "/boot", "text": "t"}]}
        plan = fixes.plan(res)
        self.assertEqual([f["key"] for f in plan], ["mount", "dkms", "initramfs", "sbctl"])
        self.assertIn("NVIDIA", plan[1]["label"])

    def test_no_initramfs_fix_without_mkinitcpio_presets(self):
        os.remove(os.path.join(self.preset_dir, "linux.preset"))
        self.assertEqual(self.keys({"boot": [{"kind": "image", "path": "/boot/initramfs-linux.img", "text": "t"}]}),
                         [])

    def test_a_newer_kernel_waiting_is_no_reinstall(self):
        res = {"boot": [{"kind": "kernel", "pkg": "linux", "text": "t"}]}
        self.assertEqual(self.keys(res), ["kernel"])
        self.patch(engine, "run", lambda cmd, **k: "linux 7.2.14.arch1-1 -> 7.2.15.arch1-1" if "-Qu" in cmd else "")
        self.assertEqual(self.keys(res), [])

    def test_a_planning_error_drops_only_that_fix(self):
        self.patch(engine, "kernel_packages", lambda: 1 / 0)
        res = {"boot": [{"kind": "unmounted", "target": "/boot", "text": "t"},
                        {"kind": "image", "path": "/boot/initramfs-linux.img", "text": "t"}]}
        self.assertEqual(self.keys(res), ["mount"])

    def test_outcome_and_journal_texts(self):
        plan = [fixes.fix("initramfs", "собрать initramfs заново", ["sudo", "mkinitcpio", "-P"], [])]
        self.assertEqual(fixes.outcome(0, [("initramfs", "ok")], plan)[0], "ok")
        kind, text = fixes.outcome(80, [("initramfs", "rolledback")], plan)
        self.assertEqual(kind, "rolledback")
        self.assertIn("вернул всё как было", text)
        self.assertEqual(fixes.outcome(81, [("initramfs", "rollbackfailed")], plan)[0], "rollbackfailed")
        self.assertEqual(fixes.outcome(1, [], plan)[0], "nothing")
        self.assertEqual(fixes.outcome(84, [("space", "notenough")])[0], "notenough")
        self.assertIn("собрать initramfs заново: не помогло", fixes.journal_text(80, [("initramfs", "rolledback")], plan))


if __name__ == "__main__":
    unittest.main(verbosity=1)
