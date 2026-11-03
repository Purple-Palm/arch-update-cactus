#!/usr/bin/env python3
"""The fixes catalogue (lib/fixes): what each fix plans for a result, and its root shell run for real against
stand-in commands (mkinitcpio, lsinitcpio, depmod, pacman, snapper… in a folder first on PATH) on files in a
temporary folder: a fix that works, one whose command fails, one whose result is wrong, and every time the files
as they were afterwards, checked byte for byte. Nothing here needs root or touches the machine."""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
os.environ["ARCH_UPDATE_REPORT_URL"] = ""          # never the real relay

import engine  # noqa: E402
import fixes  # noqa: E402

KVER = "7.2.14-arch1-1"
LTS = "6.18.60-1-lts"
REAL = {name: shutil.which(name) for name in ("cp", "stat")}    # what the stand-ins hand on to
FOLDER = re.compile(r"/tmp/arch-update-fix\.[A-Za-z0-9]{8}")


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


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def mount_point(path):
    return subprocess.run(["stat", "-c", "%m", path], capture_output=True, text=True).stdout.strip()


# mkinitcpio -p <preset>: writes the images in $T/images-<preset> (else $IMAGES) as $T/mode-mkinitcpio-<preset> (else
# $T/mode-mkinitcpio, else good) says; notes each call ($T/mkinitcpio-calls), the images and UKIs there when it began
# ($T/existed) and its umask
MKINITCPIO = r'''
p=$2
echo "$p" >>"$T/mkinitcpio-calls"
umask >"$T/umask-mkinitcpio"
echo "==> Building image from preset: /etc/mkinitcpio.d/$p.preset: 'default'"
mode=$(cat "$T/mode-mkinitcpio-$p" 2>/dev/null || cat "$T/mode-mkinitcpio" 2>/dev/null || echo good)
images=$(cat "$T/images-$p" 2>/dev/null || echo "$IMAGES")
listing=$(cat "$T/good-listing-$p" 2>/dev/null || cat "$T/good-listing")
for f in $images $(cat "$T/ukis" 2>/dev/null); do
    if [ -e "$f" ]; then echo "$f" >>"$T/existed"; fi
done
for f in $(cat "$T/ukis" 2>/dev/null); do printf 'UKI-NEW\n' >"$f"; done
for f in $images; do
    case $mode in
        good) printf 'NEW\n%s' "$listing" >"$f" ;;
        nomodules) printf 'NEW\n%s' "$(cat "$T/bare-listing")" >"$f"; echo "==> WARNING: No modules were added to the image. This is probably not what you want." ;;
        truncated) : >"$f"; echo "==> ERROR: errors were encountered during the build. The image may not be complete."; exit 1 ;;
        tmpleft) printf 'NEW-PARTIAL' >"$f.tmp"; echo "zstd: error 70 : Write error : No space left on device"; exit 1 ;;
        fullboot) printf 'NEW-PARTIAL' >"$f.tmp"; printf 'HALF' >"$f"
                  echo "zstd: error 70 : Write error : cannot write block : No space left on device"
                  echo "==> ERROR: Initcpio image generation FAILED: 'zstd' reported an error"; exit 1 ;;
        tmpfull) echo "cp: error writing '$TMPDIR/mkinitcpio.Ab12Cd/root/usr/lib/libc.so.6': No space left on device"
                 echo "==> ERROR: Initcpio image generation FAILED"; exit 1 ;;
        slow) sleep 2; printf 'NEW\n%s' "$listing" >"$f" ;;
    esac
done
echo "==> Initcpio image generation successful"
'''
# pacman's log, as pacman writes it: "[time] [ALPM] transaction started", its hooks' output as [ALPM-SCRIPTLET]
PACLOG = 'log() { printf "[2026-11-03T10:00:00+0000] %s\\n" "$*" >>"$T/pacman.log"; }\n'
# what was deleted on /: on btrfs it shows only after the next commit (btrfs filesystem sync, sync -f)
FREE = r'''if [ "$(cat "$T/fs")" = btrfs ]; then f="$T/pending"; else f="$T/committed"; fi
echo $(( $(cat "$f" 2>/dev/null || echo 0) + $1 )) >"$f"
'''
COMMIT = r'''echo $(( $(cat "$T/pending" 2>/dev/null || echo 0) + $(cat "$T/committed" 2>/dev/null || echo 0) )) >"$T/committed"
echo 0 >"$T/pending"
'''
# snapper over its configs in $T/snapper: <config>.conf (get-config), <config>.list (number,cleanup); its cleanups
# delete the oldest snapshots beyond the limit (number: NUMBER_LIMIT's upper end; timeline: <config>.timeline-keep),
# none of a config with <config>.stuck (too young), and free per-snapshot bytes each
SNAPPER = r'''S="$T/snapper"
echo "snapper $*" >>"$T/calls"
c= prev=
for a; do [ "$prev" = -c ] && c=$a; prev=$a; done
case "$*" in
    *list-configs*) echo config; cat "$S/configs" ;;
    *get-config*) echo key,value; cat "$S/$c.conf" ;;
    *" list "*) echo number,cleanup; echo "0,"; cat "$S/$c.list" ;;
    *set-config*)
        for kv; do
            case $kv in
                *=*) grep -v "^${kv%%=*}," "$S/$c.conf" >"$S/$c.conf.new"; echo "${kv%%=*},${kv#*=}" >>"$S/$c.conf.new"
                     mv "$S/$c.conf.new" "$S/$c.conf" ;;
            esac
        done ;;
    *cleanup*)
        kind=${!#}
        if [ "$kind" = number ]; then keep=$(sed -n 's/^NUMBER_LIMIT,//p' "$S/$c.conf"); keep=${keep#*-}
        else keep=$(cat "$S/$c.timeline-keep" 2>/dev/null || echo 2); fi
        if [ -e "$S/$c.stuck" ]; then keep=100000; fi
        n=$(grep -c ",$kind\$" "$S/$c.list")
        drop=$(( n > keep ? n - keep : 0 ))
        awk -F, -v k="$kind" -v d="$drop" '$2 == k && d > 0 { d--; next } { print }' "$S/$c.list" >"$S/$c.list.new"
        mv "$S/$c.list.new" "$S/$c.list"
        "$T/bin/_free" $((drop * $(cat "$S/per-snapshot")))
        ;;
esac
'''
ROOT_CONFIG = {"name": "root", "conf": {"SUBVOLUME": "/", "NUMBER_CLEANUP": "yes", "NUMBER_LIMIT": "50",
                                        "NUMBER_LIMIT_IMPORTANT": "10"}, "number": 40}
CLEANUP_NUMBER = "Удалить лишние по его же правилам (snapper cleanup number)?"
KEEP_10 = "Оставить 10 последних снимков? [y/N]"


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
        self.lock = os.path.join(self.t, "db.lck")          # pacman's lock: none, unless a test makes one
        env = {k: v for k, v in os.environ.items() if k not in ("SUDO_UID", "SUDO_USER", "DOAS_USER", "TMPDIR")}
        self.env = dict(env, PATH=f"{self.bin}:{os.environ['PATH']}", T=self.t, IMAGES=self.image)
        # stand-ins: what they do is set by files in $T (mode-*) and by $IMAGES
        self.stub("lsinitcpio", 'case $(head -n 1 "$1") in BROKEN*) exit 1 ;; esac\ntail -n +2 "$1"\n')
        self.stub("mkinitcpio", MKINITCPIO)
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

    def df_stub(self, boot_free):
        """df: the free bytes of the stand-in /boot as that bash says, plenty everywhere else."""
        self.stub("df", 'for last; do :; done\ncase $last in\n'
                        '    "$T"/boot|"$T"/boot/*) printf \'Avail\\n%s\\n\' "$(' + boot_free + ')" ;;\n'
                        '    *) printf \'Avail\\n99999999999\\n\' ;;\nesac\n')

    def cp_stub(self, refuse):
        """cp that finds no room for the files that bash test ($dst: where it writes) says."""
        self.stub("cp", 'for dst; do :; done\nif ' + refuse + '; then\n'
                        '    echo "cp: error writing \'$dst\': No space left on device" >&2\n    exit 1\nfi\n'
                        f'exec {REAL["cp"]} "$@"\n')

    def argv(self, *fixes_args):
        argv = ["bash", "-c", fixes.runner(), "arch-update-fix", self.result, self.lock]
        for fid, args in fixes_args:
            argv += [fid, *args, "--"]
        return argv

    def run_fixes(self, *fixes_args, stdin="", timeout=60):
        """The runner with these fixes: [(id, [args])]. Returns (exit status, output, results)."""
        write(self.result, "", 0o600)
        p = subprocess.run(self.argv(*fixes_args), input=stdin, capture_output=True, text=True, env=self.env,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr, fixes.read_results(self.result)

    def run_talking(self, *fixes_args, answers=(), before=None):
        """The runner in a terminal of its own, answering its questions as they come: answers [(what it asks,
        what to type)]. before: typed before anything was asked. Returns (exit status, output, results)."""
        import io
        import pexpect
        write(self.result, "", 0o600)
        argv = self.argv(*fixes_args)
        child = pexpect.spawn(argv[0], argv[1:], env=self.env, timeout=30, encoding="utf-8", dimensions=(40, 200))
        child.logfile_read = seen = io.StringIO()          # all of it, not only what came after the last answer
        if before:
            child.send(before)
        for question, typed in answers:
            child.expect_exact(question)
            time.sleep(0.4)                     # the question waits for its answer (drain is done)
            child.send(typed)
        child.expect(pexpect.EOF)
        child.close()
        return child.exitstatus, seen.getvalue(), fixes.read_results(self.result)

    def image_spec(self, path=None, kver=KVER):
        return f"i:{kver}:1:{path or self.image}"

    def two_kernels(self):
        """A second kernel, linux-lts, with an image of its own: the spec of that image."""
        self.lts = os.path.join(self.boot, "initramfs-linux-lts.img")
        write(self.lts, "OLD-LTS\n" + listing(LTS), 0o600)
        write(os.path.join(self.t, "images-linux"), self.image, 0o644)
        write(os.path.join(self.t, "images-linux-lts"), self.lts, 0o644)
        write(os.path.join(self.t, "good-listing-linux-lts"), listing(LTS), 0o644)
        return self.image_spec(self.lts, LTS)

    def leftovers(self):
        return [n for n in os.listdir("/tmp") if n.startswith("arch-update-fix.")
                and os.stat(os.path.join("/tmp", n)).st_uid == os.getuid()
                and os.stat(os.path.join("/tmp", n)).st_mtime >= self.started]

    def run(self, result=None):
        self.started = time.time() - 1
        return super().run(result)

    # ── initramfs ──

    def test_initramfs_rebuilt_and_checked(self):
        status, out, results = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(results, [("initramfs@linux", "ok")])
        self.assertTrue(read(self.image).startswith("NEW"))
        self.assertIn("собран под ядро", out)
        self.assertEqual(read(os.path.join(self.t, "existed")).split(), [self.image])     # a good one stays meanwhile
        self.assertEqual(self.leftovers(), [])

    def test_an_image_without_modules_is_put_back(self):
        """The Chromebook: mkinitcpio warns "No modules were added" and exits 0; the old image comes back."""
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "nomodules", 0o644)
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(results, [("initramfs", "rolledback")])
        self.assertEqual(digest(self.image), before)
        self.assertEqual(mode_of(self.image), 0o600)
        self.assertIn("сверил с копиями", out)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.image + ".arch-update-new"))

    def test_a_failed_build_is_put_back(self):
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "truncated", 0o644)
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(digest(self.image), before)
        self.assertEqual(results.full, [])                    # nothing ran full: not said so
        self.assertNotIn("не хватило места", out)

    def test_what_mkinitcpio_left_half_written_goes_too(self):
        """A full /boot: mkinitcpio writes <image>.tmp first and leaves it when the build fails."""
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "tmpleft", 0o644)
        status, out, _ = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(digest(self.image), before)
        self.assertFalse(os.path.exists(self.image + ".tmp"))

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

    def test_a_broken_image_goes_before_the_build_and_comes_back_if_it_fails(self):
        """The Chromebook on a small /boot: the old image (without modules, unreadable…) is no good anyway; it goes
        first (its copy is kept), so that the new one has all the room. A build that fails puts it back."""
        write(self.image, "BROKEN\n" + listing(modules=False), 0o600)        # lsinitcpio cannot read it
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "truncated", 0o644)
        status, out, _ = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertIn("Убираю негодный", out)
        self.assertFalse(os.path.exists(os.path.join(self.t, "existed")))   # gone when mkinitcpio began
        self.assertEqual(digest(self.image), before)
        self.assertEqual(mode_of(self.image), 0o600)
        # one without modules goes too, and a build that works writes the new one in its place
        write(self.image, "OLD\n" + listing(modules=False), 0o600)
        os.remove(os.path.join(self.t, "mode-mkinitcpio"))
        status, out, _ = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.OK, out)
        self.assertIn(f"в {self.image} нет модулей ядра {KVER}", out)
        self.assertFalse(os.path.exists(os.path.join(self.t, "existed")))
        self.assertTrue(read(self.image).startswith("NEW"))

    def test_a_uki_is_never_taken_away_before_the_build(self):
        uki = os.path.join(self.boot, "arch-linux.efi")
        write(uki, "", 0o644)                   # empty: no good, but not one to remove
        write(os.path.join(self.t, "ukis"), uki, 0o644)
        status, out, _ = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec(), f"u:{uki}"]))
        self.assertEqual(status, fixes.OK, out)
        self.assertIn(uki, read(os.path.join(self.t, "existed")).split())
        self.assertEqual(read(uki), "UKI-NEW\n")

    def test_the_first_preset_that_fails_ends_the_build(self):
        """No more images rebuilt after one failed: the disk may be full, a good one would be cut short."""
        lts = self.two_kernels()
        before = digest(self.image), digest(self.lts)
        for mode in ("truncated", "nomodules"):             # a failed mkinitcpio, and one that says it went wrong
            write(os.path.join(self.t, "mode-mkinitcpio-linux"), mode, 0o644)
            write(os.path.join(self.t, "mkinitcpio-calls"), "", 0o644)
            status, out, _ = self.run_fixes(("initramfs", ["p:linux", "p:linux-lts", self.image_spec(), lts]))
            self.assertEqual(status, fixes.ROLLED_BACK, out)
            self.assertEqual(read(os.path.join(self.t, "mkinitcpio-calls")).split(), ["linux"], mode)
            self.assertEqual((digest(self.image), digest(self.lts)), before, mode)

    def test_a_full_boot_is_said_and_its_room_given_back_first(self):
        """mkinitcpio's "<image>.tmp" filled /boot and the image itself was cut short: the rollback removes the .tmp
        before it puts the image back (the room it holds is what the copy needs), and says which file system ran
        full."""
        before = digest(self.image)
        write(os.path.join(self.t, "mode-mkinitcpio"), "fullboot", 0o644)
        self.df_stub('if [ -e "$IMAGES.tmp" ]; then echo 0; else echo 99999999999; fi')
        self.cp_stub('[ -e "$IMAGES.tmp" ] && [[ $dst == "$T"/boot/* ]]')
        plan = [fixes.fix("initramfs", "собрать заново initramfs ядра linux (mkinitcpio)", ["sudo"], [],
                          scope="linux")]
        status, out, results = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(results, [("initramfs@linux", "rolledback")])
        where = mount_point(self.boot)
        self.assertEqual(results.full, [where])
        self.assertIn(f"на {where} не хватило места для initramfs: свободно 0 КБ", out)
        self.assertEqual(digest(self.image), before)
        self.assertFalse(os.path.exists(self.image + ".tmp"))
        self.assertEqual(fixes.outcome(status, results, plan),
                         ("rolledback", f"«собрать заново initramfs ядра linux (mkinitcpio)» не помогло: на {where} "
                                        "не хватило места — вернул всё как было"))

    def test_a_build_folder_that_ran_full_is_said_too(self):
        """/tmp ran full while mkinitcpio built the image: it removed its build folder as it stopped, so nothing is
        full any more; the message names it."""
        build = os.path.join(self.t, "build")
        os.makedirs(build)
        self.env["TMPDIR"] = build
        write(os.path.join(self.t, "mode-mkinitcpio"), "tmpfull", 0o644)
        status, out, results = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(results.full, [mount_point(build)])

    def test_no_room_for_copies_changes_nothing(self):
        before = digest(self.image)
        self.stub("df", 'printf "Avail\\n1024\\n"\n')
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.NO_BACKUP, out)
        self.assertEqual(results, [("initramfs", "nobackup")])
        self.assertIn("ничего не менял", out)
        self.assertEqual(digest(self.image), before)

    def test_when_putting_back_fails_the_copies_stay_and_it_says_how_to_put_them_back(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        lts = self.two_kernels()
        # mkinitcpio writes the new image, then the folder may not be written any more
        self.stub("mkinitcpio", f'printf "NEW\\n%s" "$(cat "$T/bare-listing")" >"{self.image}"\n'
                                f'chmod 0400 "{self.image}"; chmod 0555 "{self.boot}"\n'
                                'echo "No modules were added to the image"\n')
        status, out, results = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]),
                                              ("initramfs@linux-lts", ["p:linux-lts", lts]))
        self.assertEqual(status, fixes.ROLLBACK_FAILED, out)
        # the machine is not as it was: nothing more is done, for any kernel
        self.assertEqual(results, [("initramfs@linux", "rollbackfailed"), ("initramfs@linux-lts", "skipped")])
        kept = [os.path.join("/tmp", n) for n in self.leftovers()]
        self.assertEqual(len(kept), 1)
        self.assertEqual(results.kept, kept[0])
        self.assertIn(f"копии остались в {kept[0]}", out)
        self.assertIn(f"sudo cp -- {kept[0]}/0 {self.image}", out)          # what puts it back by hand
        self.assertTrue(read(os.path.join(kept[0], "0")).startswith("OLD"))
        self.assertEqual(read(os.path.join(kept[0], "0.path")), self.image + "\n")
        self.assertEqual(read(os.path.join(kept[0], "1.path")), self.image + ".tmp\n")
        self.assertEqual((mode_of(kept[0]), mode_of(os.path.join(kept[0], "0.sum")),
                          mode_of(os.path.join(kept[0], "0.path"))), (0o700, 0o600, 0o600))
        shutil.rmtree(kept[0])

    def test_with_no_room_beside_a_file_it_comes_back_with_its_mode(self):
        """Putting back writes beside the file first; where that cannot be (a full partition), the file the fix
        left goes and the copy takes its place, with the copy's mode and time — when the copy then fits."""
        args = self.depmod_setup()
        os.utime(self.ld, (1_700_000_000, 1_700_000_000))
        self.stub("depmod", "exit 1\n")
        self.stub("ldconfig", f': >"{self.ld}"\nexit 1\n')
        self.cp_stub('[[ $dst == *.arch-update-new ]]')        # nothing can be written beside it
        status, out, _ = self.run_fixes(("depmod", args))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(read(self.ld), "LDCACHE-OLD")
        self.assertEqual(mode_of(self.ld), 0o644)
        self.assertEqual(int(os.stat(self.ld).st_mtime), 1_700_000_000)

    def test_a_file_the_copy_would_not_fit_in_place_of_stays(self):
        """No room beside the file, and not even in its place: the file the fix left stays (better than none), the
        copy stays in /tmp, and it says what to type to put it back."""
        write(self.image, "OLD\n" + listing() + "x" * 3_000_000 + "\n", 0o600)
        write(os.path.join(self.t, "mode-mkinitcpio"), "nomodules", 0o644)
        self.df_stub("echo 0")                  # /boot full
        self.cp_stub('[[ $dst == "$T"/boot/* ]]')
        status, out, results = self.run_fixes(("initramfs@linux", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLBACK_FAILED, out)
        self.assertTrue(read(self.image).startswith("NEW"))           # what mkinitcpio made: still there
        self.assertIn(f"на {mount_point(self.boot)} нет места, чтобы вернуть {self.image}", out)
        kept = results.kept
        self.assertTrue(kept and FOLDER.fullmatch(kept), results.extra)
        self.assertIn(f"sudo cp -- {kept}/0 {self.image}", out)
        self.assertTrue(read(os.path.join(kept, "0")).startswith("OLD"))
        shutil.rmtree(kept)

    def test_ctrl_c_does_not_cut_a_fix_off(self):
        import pexpect
        write(os.path.join(self.t, "mode-mkinitcpio"), "slow", 0o644)
        argv = self.argv(("initramfs", ["p:linux", self.image_spec()]))
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

    def test_depmod_writes_the_module_list_readable_for_all(self):
        """The lists come out 0644, as pacman's own depmod writes them: as a user, modinfo and lspci -k look modules
        up in them (the runner's own copies stay root's only)."""
        args = self.depmod_setup()
        self.stub("depmod", f'umask >"$T/umask-depmod"\necho index >"{self.mods}/$1/modules.dep.bin"\n'
                            f'echo dep >"{self.mods}/$1/modules.dep"\n')
        self.stub("ldconfig", f'echo LDCACHE-NEW >"{self.ld}"\n')
        status, out, results = self.run_fixes(("depmod", args))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(results, [("depmod", "ok")])
        self.assertEqual(mode_of(os.path.join(self.mods, KVER, "modules.dep.bin")), 0o644)
        self.assertEqual(read(os.path.join(self.t, "umask-depmod")).strip(), "0022")

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
        """depmod is for the whole machine: when it was not done, no kernel's initramfs is built."""
        args = self.depmod_setup()
        lts = self.two_kernels()
        self.stub("depmod", "exit 1\n")
        self.stub("ldconfig", "exit 0\n")
        status, _, results = self.run_fixes(("depmod", args), ("initramfs@linux", ["p:linux", self.image_spec()]),
                                            ("initramfs@linux-lts", ["p:linux-lts", lts]))
        self.assertEqual(status, fixes.ROLLED_BACK)
        self.assertEqual(results, [("depmod", "rolledback"), ("initramfs@linux", "skipped"),
                                   ("initramfs@linux-lts", "skipped")])
        self.assertTrue(read(self.image).startswith("OLD"))

    # ── kernel ──

    def kernel_log(self, *earlier):
        """pacman's log (a stand-in) with what earlier transactions wrote: its g: spec."""
        write(os.path.join(self.t, "pacman.log"),
              "".join(f"[2026-11-03T09:00:00+0000] {line}\n" for line in earlier), 0o644)
        return f"g:{os.path.join(self.t, 'pacman.log')}"

    def test_the_kernel_again_from_the_cache(self):
        pkg = os.path.join(self.t, "linux-7.2.14.arch1-1-x86_64.pkg.tar.zst")
        write(pkg, "pkg", 0o644)
        g = self.kernel_log()
        self.stub("pacman", PACLOG + f'echo "$@" >"$T/pacman-args"\nlog "[ALPM] transaction started"\n'
                                     f'echo KERNEL-NEW >"{self.kernel}"\n'
                                     'log "[ALPM] reinstalled linux (7.2.14.arch1-1)"\n'
                                     'log "[ALPM] transaction completed"\n'
                                     'log "[ALPM] running \'90-mkinitcpio-install.hook\'..."\n'
                                     f'printf "NEW\\n%s" "$(cat "$T/good-listing")" >"{self.image}"\n'
                                     'log "[ALPM-SCRIPTLET] ==> Initcpio image generation successful"\n')
        status, out, results = self.run_fixes(("kernel@linux", [f"c:{pkg}", g, f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(results, [("kernel@linux", "ok")])
        self.assertEqual(read(os.path.join(self.t, "pacman-args")).split(), ["-U", pkg])

    def test_a_reinstall_answered_no_changes_nothing_and_says_so(self):
        """pacman asked and the answer was no (or its download failed): it began no transaction, nothing was
        changed and nothing is put back — not "it did not help". Only what pacman wrote while this fix ran counts:
        an earlier transaction's lines in its log are not this one's."""
        g = self.kernel_log("[ALPM] transaction started", "[ALPM-SCRIPTLET] ==> WARNING: errors were encountered "
                            "during the build. The image may not be complete.", "[ALPM] transaction completed")
        before = digest(self.kernel), digest(self.image)
        self.stub("pacman", PACLOG + 'log "[PACMAN] Running \'pacman -S linux\'"\n'
                                     'echo ":: Proceed with installation? [Y/n] n"\nexit 1\n')
        status, out, results = self.run_fixes(("kernel@linux", ["r:linux", g, f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual(status, fixes.UNCHANGED, out)
        self.assertEqual(results, [("kernel@linux", "unchanged")])
        self.assertIn("pacman ничего не поставил — ничего не менял", out)
        self.assertNotIn("Не помогло", out)
        self.assertEqual((digest(self.kernel), digest(self.image)), before)
        self.assertEqual(self.leftovers(), [])

    def test_a_reinstall_that_failed_halfway_is_put_back(self):
        g = self.kernel_log()
        before = digest(self.kernel), digest(self.image)
        self.stub("pacman", PACLOG + f'log "[ALPM] transaction started"\n: >"{self.kernel}"\n'
                                     'log "[ALPM] transaction failed"\nexit 1\n')
        status, out, results = self.run_fixes(("kernel@linux", ["r:linux", g, f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual((status, results), (fixes.ROLLED_BACK, [("kernel@linux", "rolledback")]), out)
        self.assertEqual((digest(self.kernel), digest(self.image)), before)
        # with no log to tell, files that were changed are what counts: put back too
        self.stub("pacman", f': >"{self.kernel}"\necho ":: Proceed with installation? [Y/n] n"\nexit 1\n')
        status, _, _ = self.run_fixes(("kernel", ["r:linux", f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual(status, fixes.ROLLED_BACK)
        self.assertEqual((digest(self.kernel), digest(self.image)), before)

    def test_a_kernel_back_with_mkinitcpio_errors_is_incomplete_and_kept(self):
        """pacman went through, but its mkinitcpio hook reported errors (a hook in HOOKS that is not installed):
        the kernel file is back, its initramfs may not be whole. Not put back (that would take the kernel file away
        again), and not called done."""
        os.remove(self.kernel)
        g = self.kernel_log()
        self.stub("pacman", PACLOG + f'log "[ALPM] transaction started"\necho KERNEL-NEW >"{self.kernel}"\n'
                                     'log "[ALPM] transaction completed"\n'
                                     'log "[ALPM] running \'90-mkinitcpio-install.hook\'..."\n'
                                     f'printf "NEW\\n%s" "$(cat "$T/good-listing")" >"{self.image}"\n'
                                     'log "[ALPM-SCRIPTLET] ==> ERROR: Hook \'plymouth\' cannot be found"\n'
                                     'log "[ALPM-SCRIPTLET] ==> WARNING: errors were encountered during the build. '
                                     'The image may not be complete."\n'
                                     'echo "error: command failed to execute correctly" >&2\n')
        status, out, results = self.run_fixes(("kernel@linux", ["r:linux", g, f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual(status, fixes.INCOMPLETE, out)
        self.assertEqual(results, [("kernel@linux", "incomplete")])
        self.assertEqual(read(self.kernel), "KERNEL-NEW\n")
        self.assertIn("НЕ перезагружайся", out)
        self.assertNotIn("Не помогло", out)
        self.assertEqual(results.extra, {})

    def test_a_kernel_back_with_another_hook_failing_is_incomplete_too(self):
        """sbctl's hook could not sign the new kernel: pacman says "command failed to execute correctly" on stderr
        only (into no log) and exits 0. Not done, not put back; and not blamed on mkinitcpio."""
        os.remove(self.kernel)
        g = self.kernel_log()
        self.stub("pacman", PACLOG + f'log "[ALPM] transaction started"\necho KERNEL-NEW >"{self.kernel}"\n'
                                     'log "[ALPM] transaction completed"\nlog "[ALPM] running \'zz-sbctl.hook\'..."\n'
                                     'log "[ALPM-SCRIPTLET] ✗ Failed to sign /boot/vmlinuz-linux"\n'
                                     'echo "error: command failed to execute correctly" >&2\necho "pacman goes on"\n')
        plan = [fixes.fix("kernel", "переустановить ядро linux", ["sudo"], [], scope="linux")]
        status, out, results = self.run_fixes(("kernel@linux", ["r:linux", g, f"k:{self.kernel}", self.image_spec()]))
        self.assertEqual((status, results), (fixes.INCOMPLETE, [("kernel@linux", "incomplete")]), out)
        self.assertEqual(results.extra, {"kernel@linux": ["hook"]})
        self.assertEqual(read(self.kernel), "KERNEL-NEW\n")
        self.assertIn("error: command failed to execute correctly", out)       # still shown in the terminal
        self.assertIn("pacman goes on", out)
        self.assertEqual(fixes.outcome(status, results, plan),
                         ("incomplete", "«переустановить ядро linux»: файл ядра на месте, но хук pacman закончился с "
                                        "ошибкой — загрузка может не пройти"))

    def test_pacman_installing_nothing_for_one_kernel_leaves_the_other_alone(self):
        lts = self.two_kernels()
        g = self.kernel_log()
        self.stub("pacman", 'echo ":: Proceed with installation? [Y/n] n"\nexit 1\n')
        status, out, results = self.run_fixes(
            ("kernel@linux", ["r:linux", g, f"k:{self.kernel}", self.image_spec()]),
            ("initramfs@linux", ["p:linux", self.image_spec()]), ("initramfs@linux-lts", ["p:linux-lts", lts]))
        self.assertEqual(status, fixes.UNCHANGED, out)
        self.assertEqual(results, [("kernel@linux", "unchanged"), ("initramfs@linux", "skipped"),
                                   ("initramfs@linux-lts", "ok")])
        self.assertTrue(read(self.lts).startswith("NEW"))
        self.assertTrue(read(self.image).startswith("OLD"))

    # ── dkms, sbctl, mount ──

    def test_dkms_built_or_not(self):
        self.stub("dkms", 'case $1 in autoinstall) exit 0 ;; status) cat "$T/dkms-status" ;; esac\n')
        write(os.path.join(self.t, "dkms-status"), f"nvidia/615.78, {KVER}, x86_64: installed\n", 0o644)
        self.assertEqual(self.run_fixes(("dkms@linux", [f"nvidia:{KVER}"]))[0], fixes.OK)
        write(os.path.join(self.t, "dkms-status"), "nvidia/615.78: added\n", 0o644)
        status, out, _ = self.run_fixes(("dkms@linux", [f"nvidia:{KVER}"]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertIn("так и не собран", out)

    def test_a_kernel_whose_module_does_not_build_holds_up_only_its_own_initramfs(self):
        """linux-lts has no NVIDIA module (no headers for it): its initramfs is not built; linux's is."""
        lts = self.two_kernels()
        before = digest(self.lts)
        self.stub("dkms", 'case $1 in autoinstall) exit 1 ;; status) echo "nvidia/615.78: added" ;; esac\n')
        status, out, results = self.run_fixes(
            ("dkms@linux-lts", [f"nvidia:{LTS}"]), ("initramfs@linux", ["p:linux", self.image_spec()]),
            ("initramfs@linux-lts", ["p:linux-lts", lts]))
        self.assertEqual(status, fixes.ROLLED_BACK, out)
        self.assertEqual(results, [("dkms@linux-lts", "rolledback"), ("initramfs@linux", "ok"),
                                   ("initramfs@linux-lts", "skipped")])
        self.assertTrue(read(self.image).startswith("NEW"))
        self.assertEqual(digest(self.lts), before)
        self.assertEqual(read(os.path.join(self.t, "mkinitcpio-calls")).split(), ["linux"])

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

    def space_machine(self, base, fs="ext4", pacman=0, journal=0, configs=(), per=10 ** 8, snapper=True):
        """A stand-in machine for the space fix: / with base bytes free; pacman -Sc and the journal free what they
        say, snapper's cleanups per bytes a snapshot (configs: [{"name", "conf": {key: value}, "number": snapshots
        it cleans by number, "timeline": by age, "stuck": none of them old enough}]); on btrfs (fs) all of it shows
        only after a commit."""
        for name in ("calls", "pending", "committed"):
            if os.path.exists(os.path.join(self.t, name)):
                os.remove(os.path.join(self.t, name))
        write(os.path.join(self.t, "fs"), fs + "\n", 0o644)
        write(os.path.join(self.t, "base"), f"{base}\n", 0o644)
        self.stub("_free", FREE)
        self.stub("_commit", COMMIT)
        self.stub("df", r'''for last; do :; done
case "$*" in *--output=source*)
    if grep -qxF -- "$last" "$T/other-disk" 2>/dev/null; then printf 'Filesystem\n/dev/other\n'; else printf 'Filesystem\n/dev/root\n'; fi
    exit 0 ;;
esac
if [ "$last" = / ]; then printf 'Avail\n%s\n' $(( $(cat "$T/base") + $(cat "$T/committed" 2>/dev/null || echo 0) ))
else printf 'Avail\n99999999999\n'; fi
''')
        self.stub("stat", f'if [ "$*" = "-f -c %T /" ]; then cat "$T/fs"; exit 0; fi\nexec {REAL["stat"]} "$@"\n')
        self.stub("btrfs", 'echo "btrfs $*" >>"$T/calls"\ncase "$1 $2" in "filesystem sync") "$T/bin/_commit" ;; esac\n')
        self.stub("sync", 'if [ "${1:-}" = -f ]; then echo "sync $*" >>"$T/calls"; "$T/bin/_commit"; fi\n')
        self.stub("pacman", f'echo "pacman $*" >>"$T/calls"\n"$T/bin/_free" {pacman}\n')
        self.stub("journalctl", f'echo "journalctl $*" >>"$T/calls"\n"$T/bin/_free" {journal}\n')
        if snapper:
            self.stub("snapper", SNAPPER)
        elif os.path.exists(os.path.join(self.bin, "snapper")):
            os.remove(os.path.join(self.bin, "snapper"))
        s = os.path.join(self.t, "snapper")
        shutil.rmtree(s, ignore_errors=True)
        os.makedirs(s)
        write(os.path.join(s, "configs"), "".join(c["name"] + "\n" for c in configs), 0o644)
        write(os.path.join(s, "per-snapshot"), f"{per}\n", 0o644)
        for c in configs:
            write(os.path.join(s, c["name"] + ".conf"), "".join(f"{k},{v}\n" for k, v in c["conf"].items()), 0o644)
            rows = [f"{i},number" for i in range(1, c.get("number", 0) + 1)]
            rows += [f"{100 + i},timeline" for i in range(1, c.get("timeline", 0) + 1)]
            write(os.path.join(s, c["name"] + ".list"), "".join(r + "\n" for r in rows), 0o644)
            if c.get("stuck"):
                write(os.path.join(s, c["name"] + ".stuck"), "", 0o644)

    def calls(self):
        path = os.path.join(self.t, "calls")
        return read(path) if os.path.exists(path) else ""

    def test_space_freed_without_touching_snapshots(self):
        want = str(2 * 10 ** 9)
        self.space_machine(10 ** 9, journal=2 * 10 ** 9, configs=[ROOT_CONFIG])
        status, out, results = self.run_fixes(("space", [want, want]))
        self.assertEqual((status, results), (fixes.OK, [("space", "ok")]), out)
        self.assertIn("pacman -Sc --noconfirm", self.calls())
        self.assertIn("journalctl --vacuum-size=200M", self.calls())
        self.assertNotIn("snapper", self.calls())            # room enough already: snapshots not even looked at
        self.assertIn("свободно 953 МБ, нужно 1,9 ГБ — не хватает 953 МБ", out)
        # pacman's cache alone made the room: the journal stays as it is
        self.space_machine(10 ** 9, pacman=2 * 10 ** 9, journal=10 ** 9, configs=[ROOT_CONFIG])
        status, out, _ = self.run_fixes(("space", [want, want]))
        self.assertEqual(status, fixes.OK, out)
        self.assertNotIn("journalctl", self.calls())

    def test_with_room_enough_from_the_start_nothing_is_deleted(self):
        want = str(2 * 10 ** 9)
        self.space_machine(3 * 10 ** 9, configs=[ROOT_CONFIG])
        status, out, results = self.run_fixes(("space", [want, want]))
        self.assertEqual((status, results), (fixes.OK, [("space", "ok")]), out)
        self.assertIn("места уже хватает, ничего не удаляю", out)
        self.assertEqual(self.calls(), "")

    def test_snapshots_go_only_when_the_user_says_yes(self):
        want = str(2 * 10 ** 9)
        self.space_machine(10 ** 9, configs=[ROOT_CONFIG])
        status, out, results = self.run_talking(("space", [want, want]), answers=[(CLEANUP_NUMBER, "\r")])
        self.assertEqual((status, results), (fixes.NOT_ENOUGH, [("space", "notenough")]), out)
        self.assertNotIn("cleanup number", self.calls())
        # yes; its own limit (50) deletes none of the 40, and yes again to keeping only the last 10
        self.space_machine(10 ** 9, configs=[ROOT_CONFIG])
        status, out, results = self.run_talking(("space", [want, want]), answers=[(CLEANUP_NUMBER, "y\r"),
                                                                                  (KEEP_10, "y\r")])
        self.assertEqual((status, results), (fixes.OK, [("space", "ok")]), out)
        self.assertIn("snapper -c root cleanup number", self.calls())
        self.assertIn("snapper -c root set-config NUMBER_LIMIT=10 NUMBER_LIMIT_IMPORTANT=5", self.calls())
        self.assertIn("(NUMBER_LIMIT=10, NUMBER_LIMIT_IMPORTANT=5)", out)

    def test_a_key_pressed_before_the_question_is_no_yes(self):
        want = str(2 * 10 ** 9)
        self.space_machine(10 ** 9, configs=[ROOT_CONFIG])
        status, out, _ = self.run_talking(("space", [want, want]), before="y\r", answers=[(CLEANUP_NUMBER, "\r")])
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        self.assertNotIn("cleanup number", self.calls())

    def test_snapshots_snapper_does_not_clean_itself_are_left_alone(self):
        self.space_machine(10 ** 9, configs=[{"name": "root", "conf": {"NUMBER_CLEANUP": "no"}, "number": 40}])
        status, out, _ = self.run_fixes(("space", [str(2 * 10 ** 9), str(2 * 10 ** 9)]))
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        self.assertIn("NUMBER_CLEANUP не yes", out)
        self.assertNotIn("[y/N]", out)
        self.assertNotIn("cleanup", self.calls())

    def test_on_btrfs_what_was_freed_counts_at_once(self):
        """btrfs gives back what deleted snapshots held only when it commits: the look after the cleanup commits
        first, so snapper's own cleanup that made room enough is seen at once — no needless question to cut the
        snapshots down to 10, no "too little" that is not so."""
        want = str(2 * 10 ** 9)
        config = dict(ROOT_CONFIG, conf=dict(ROOT_CONFIG["conf"], NUMBER_LIMIT="20"), number=30)
        self.space_machine(10 ** 9, fs="btrfs", configs=[config])
        status, out, results = self.run_talking(("space", [want, want]), answers=[(CLEANUP_NUMBER, "y\r")])
        self.assertEqual((status, results), (fixes.OK, [("space", "ok")]), out)
        self.assertNotIn("Оставить", out)
        calls = self.calls()
        self.assertIn("btrfs subvolume sync /", calls)
        self.assertGreater(calls.rindex("btrfs filesystem sync /"), calls.index("snapper -c root cleanup number"))
        self.assertIn("освободил 953 МБ", out)
        # and what pacman -Sc deleted: the journal is not touched for nothing
        self.space_machine(10 ** 9, fs="btrfs", pacman=2 * 10 ** 9, configs=[ROOT_CONFIG])
        status, out, _ = self.run_fixes(("space", [want, want]))
        self.assertEqual(status, fixes.OK, out)
        self.assertNotIn("journalctl", self.calls())

    def test_timeline_snapshots_are_offered_too(self):
        """archinstall's snapper setup: hourly snapshots cleaned up by age, no snap-pac."""
        want = str(2 * 10 ** 9)
        config = {"name": "root", "conf": {"NUMBER_CLEANUP": "no", "TIMELINE_CLEANUP": "yes"}, "timeline": 14}
        self.space_machine(10 ** 9, configs=[config])
        status, out, results = self.run_talking(("space", [want, want]), answers=[(
            "Удалить лишние по его же правилам (snapper cleanup timeline)?", "y\r")])
        self.assertEqual((status, results), (fixes.OK, [("space", "ok")]), out)
        self.assertIn("которые он удаляет сам: 14, это снимки по расписанию", out)
        self.assertIn("snapper -c root cleanup timeline", self.calls())
        self.assertNotIn("cleanup number", self.calls())
        # both kinds: both counted, both cleaned
        config = dict(ROOT_CONFIG, conf=dict(ROOT_CONFIG["conf"], TIMELINE_CLEANUP="yes"), number=30, timeline=6)
        self.space_machine(10 ** 9, configs=[config])
        status, out, _ = self.run_talking(("space", [want, want]), answers=[
            ("(snapper cleanup number и timeline)?", "y\r"), (KEEP_10, "n\r")])
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        self.assertIn("которые он удаляет сам: 36 — 30 по номеру (держит до 50) и 6 по расписанию", out)
        self.assertIn("snapper -c root cleanup number", self.calls())
        self.assertIn("snapper -c root cleanup timeline", self.calls())
        self.assertNotIn("set-config", self.calls())
        # snapper's own template, as archinstall leaves it: both cleanups on, but only hourly snapshots there
        config = dict(ROOT_CONFIG, conf=dict(ROOT_CONFIG["conf"], TIMELINE_CLEANUP="yes"), number=0, timeline=14)
        self.space_machine(10 ** 9, configs=[config])
        status, out, _ = self.run_talking(("space", [want, want]), answers=[
            ("Удалить лишние по его же правилам (snapper cleanup timeline)?", "y\r")])
        self.assertEqual(status, fixes.OK, out)
        self.assertIn("которые он удаляет сам: 14, это снимки по расписанию", out)
        self.assertNotIn("cleanup number", self.calls())

    def test_limits_are_only_lowered_and_for_every_config(self):
        """Every config is asked about, not only root; a limit already lower stays as it is (2 important ones stay
        2), a range keeps its lower end, and a config with nothing to lower is not asked."""
        want = str(10 ** 12)
        configs = [
            {"name": "root", "conf": {"NUMBER_CLEANUP": "yes", "NUMBER_LIMIT": "50", "NUMBER_LIMIT_IMPORTANT": "2"},
             "number": 40},
            {"name": "home", "conf": {"NUMBER_CLEANUP": "yes", "NUMBER_LIMIT": "2-50",
                                      "NUMBER_LIMIT_IMPORTANT": "10"}, "number": 30},
            {"name": "data", "conf": {"NUMBER_CLEANUP": "yes", "NUMBER_LIMIT": "8", "NUMBER_LIMIT_IMPORTANT": "3"},
             "number": 20, "stuck": True},
            {"name": "var", "conf": {"NUMBER_CLEANUP": "yes", "NUMBER_LIMIT": "10", "NUMBER_LIMIT_IMPORTANT": "10"},
             "number": 20, "stuck": True}]
        self.space_machine(10 ** 9, configs=configs, per=1)
        status, out, _ = self.run_talking(("space", [want, want]), answers=[
            (CLEANUP_NUMBER, "y\r"), (KEEP_10, "y\r"),                  # root
            (CLEANUP_NUMBER, "y\r"), (KEEP_10, "y\r"),                  # home
            (CLEANUP_NUMBER, "y\r"),                                    # data: nothing to lower, not asked
            (CLEANUP_NUMBER, "y\r"), ("Оставить 5 последних важных снимков? [y/N]", "n\r")])    # var
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        calls = self.calls().splitlines()
        self.assertIn("snapper -c root set-config NUMBER_LIMIT=10", calls)
        self.assertIn("snapper -c home set-config NUMBER_LIMIT=2-10 NUMBER_LIMIT_IMPORTANT=5", calls)
        self.assertFalse([c for c in calls if "-c data set-config" in c or "-c var set-config" in c], calls)
        self.assertIn("(NUMBER_LIMIT=10).", out)
        self.assertIn("(NUMBER_LIMIT=2-10, NUMBER_LIMIT_IMPORTANT=5).", out)
        self.assertIn("(NUMBER_LIMIT_IMPORTANT=5).", out)
        self.assertEqual(out.count(KEEP_10), 2)

    def test_snapshots_on_another_disk_are_not_offered(self):
        other = {"name": "data", "conf": {"SUBVOLUME": self.t, "NUMBER_CLEANUP": "yes", "NUMBER_LIMIT": "50"},
                 "number": 40}
        self.space_machine(10 ** 9, configs=[other])
        write(os.path.join(self.t, "other-disk"), self.t + "\n", 0o644)
        status, out, _ = self.run_fixes(("space", [str(2 * 10 ** 9), str(2 * 10 ** 9)]))
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        self.assertIn("снимки snapper (data) лежат на другом диске", out)
        self.assertNotIn("[y/N]", out)

    def test_too_little_says_how_much_and_only_what_this_machine_has(self):
        """"нужно 2,6 ГБ, свободно 2,6 ГБ" no longer: how much is missing is said; the hints name snapper only with
        snapper, Timeshift only with Timeshift, yay's cache only with yay."""
        need = 2662 * 2 ** 20
        self.space_machine(2632 * 2 ** 20, snapper=False)
        status, out, _ = self.run_fixes(("space", [str(need), str(need)]))
        self.assertEqual(status, fixes.NOT_ENOUGH, out)
        self.assertIn("места всё ещё мало: свободно 2,6 ГБ, нужно хотя бы 2,6 ГБ — не хватает 30 МБ", out)
        for absent, name in (("snapper list", "snapper"), ("timeshift", "timeshift"), ("~/.cache/yay", "yay"),
                             ("~/.cache/paru", "paru")):
            if not shutil.which(name):
                self.assertNotIn(absent, out)
        self.assertIn("ненужные пакеты: pacman -Qdtq", out)
        self.assertIn("du -sh ~/* | sort -h", out)
        self.space_machine(2632 * 2 ** 20, configs=[])
        self.stub("timeshift", "exit 0\n")
        self.stub("yay", "exit 0\n")
        status, out, _ = self.run_fixes(("space", [str(need), str(need)]))
        self.assertIn("снимки snapper: sudo snapper list", out)
        self.assertIn("снимки Timeshift: sudo timeshift --list", out)
        self.assertIn("кэш yay: ~/.cache/yay", out)

    def test_room_for_the_fixes_is_enough_for_them_to_go_on(self):
        """The Chromebook: not 2 GB free after all, but 1 GB: depmod and mkinitcpio need far less, they run."""
        self.space_machine(10 ** 9, snapper=False)
        self.stub("mountpoint", 'test -e "$T/mounted"\n')
        self.stub("mount", ': >"$T/mounted"\n')
        status, out, results = self.run_fixes(("space", [str(2 * 10 ** 9), str(512 * 2 ** 20)]), ("mount", ["/boot"]))
        self.assertEqual(status, fixes.OK, out)
        self.assertEqual(results, [("space", "partial"), ("mount", "ok")])
        self.assertIn("для исправлений хватает, для обновления нужно около", out)
        # below even that: the fixes after it do not run (they would fail for want of room)
        os.remove(os.path.join(self.t, "mounted"))
        self.space_machine(100 * 2 ** 20, snapper=False)
        status, out, results = self.run_fixes(("space", [str(2 * 10 ** 9), str(512 * 2 ** 20)]), ("mount", ["/boot"]))
        self.assertEqual((status, results), (fixes.NOT_ENOUGH, [("space", "notenough"), ("mount", "skipped")]))

    # ── the driver ──

    def test_unknown_fix_and_pacman_at_work(self):
        status, out, results = self.run_fixes(("nosuchfix", []))
        self.assertEqual((status, results), (fixes.NO_BACKUP, [("nosuchfix", "unknown")]))
        self.stub("pgrep", "echo 123\n")
        # a pacman somewhere (a container's own) without this machine's lock is not this machine's pacman
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual(status, fixes.OK, out)
        write(self.lock, "", 0o644)
        before = digest(self.image)
        status, out, results = self.run_fixes(("initramfs", ["p:linux", self.image_spec()]))
        self.assertEqual((status, results), (fixes.REFUSED, [("initramfs", "refused")]))
        self.assertEqual(results.extra, {"initramfs": ["pacman"]})            # why, for the texts after it
        self.assertIn("работает pacman", out)
        self.assertEqual(digest(self.image), before)

    def test_the_result_file_is_written_only_when_it_is_the_users_own_plain_file(self):
        target = os.path.join(self.t, "somebody-elses")
        write(target, "", 0o644)
        os.symlink(target, self.result + ".link")
        argv = self.argv(("nosuchfix", []))
        argv[argv.index(self.result)] = self.result + ".link"
        subprocess.run(argv, capture_output=True, env=self.env, timeout=30)
        self.assertEqual(read(target), "")                    # a link put in its place: nothing written through it
        write(self.result, "", 0o600)
        subprocess.run(self.argv(("nosuchfix", [])), capture_output=True, env=dict(self.env, SUDO_UID="12345"),
                       timeout=30)
        self.assertEqual(read(self.result), "")               # not owned by the user who ran sudo

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
        self.patch(fixes.space, "free", lambda path="/": 50 * 2 ** 30)       # room enough, unless a test says not

    def tearDown(self):
        for (obj, name), value in self.saved.items():
            setattr(obj, name, value)
        shutil.rmtree(self.t, ignore_errors=True)

    def patch(self, obj, name, value):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, value)

    def keys(self, res):
        return [f["key"] for f in fixes.plan(res)]

    def ids(self, res):
        return [f["id"] for f in fixes.plan(res)]

    def two_kernels(self):
        """linux and linux-lts, each with a preset (linux-lts with its fallback image too)."""
        write(os.path.join(self.preset_dir, "linux-lts.preset"), "", 0o644)
        self.patch(engine, "kernel_packages", lambda: {"linux": KVER, "linux-lts": LTS})
        files = {"linux": [("kernel", "/boot/vmlinuz-linux"), ("image", "/boot/initramfs-linux.img")],
                 "linux-lts": [("kernel", "/boot/vmlinuz-linux-lts"), ("image", "/boot/initramfs-linux-lts.img"),
                               ("image", "/boot/initramfs-linux-lts-fallback.img")]}
        self.patch(engine, "preset_files", lambda pkg: files.get(pkg, []))
        self.patch(engine, "installed_versions", lambda: {"linux": "7.2.14.arch1-1", "linux-lts": "6.18.60-1"})

    def test_the_chromebook(self):
        """depmod failed on a full disk, mkinitcpio built an image without modules."""
        res = {"problems": [engine.DISK_FULL],
               "boot": [{"kind": "depmod", "kvers": [KVER], "text": "depmod"},
                        {"kind": "initcpio", "text": "в initramfs не попал ни один модуль"}]}
        self.patch(fixes.space, "free", lambda path="/": 300 * 2 ** 20)      # its disk, nearly full
        plan = fixes.plan(res)
        self.assertEqual([f["id"] for f in plan], ["space", "depmod", "initramfs@linux"])
        # 2 GiB wanted for the update, but the boot fixes go on from 512 MiB
        self.assertEqual(plan[0]["args"], [str(fixes.space.AFTER_FULL), str(fixes.space.LOW)])
        self.assertEqual(plan[1]["args"][-1], KVER)
        self.assertEqual(plan[2]["args"], ["p:linux", f"i:{KVER}:1:/boot/initramfs-linux.img"])
        self.assertEqual((plan[2]["label"], plan[2]["how"]),
                         ("собрать заново initramfs ядра linux (mkinitcpio)", "sudo mkinitcpio -p linux"))
        argv = fixes.command(plan, "/home/u/.local/state/arch-update-auto/fix-result-x")
        self.assertEqual(argv[:3], ["sudo", "bash", "-c"])
        self.assertEqual(argv[4:6], ["arch-update-fix", "/home/u/.local/state/arch-update-auto/fix-result-x"])
        self.assertEqual(argv.count("--"), 3)
        self.assertIn("initramfs@linux", argv)

    def test_before_the_update_the_disk_was_too_small(self):
        self.patch(fixes.space, "free", lambda path="/": 10 ** 9)
        plan = fixes.plan({"space": {"need": 3 * 10 ** 9, "free": 10 ** 9}})
        self.assertEqual([(f["key"], f["args"]) for f in plan], [("space", [str(3 * 10 ** 9)] * 2)])
        # room was made meanwhile (by hand): nothing to free, F is not offered
        self.patch(fixes.space, "free", lambda path="/": 4 * 10 ** 9)
        self.assertEqual(fixes.plan({"space": {"need": 3 * 10 ** 9, "free": 10 ** 9}}), [])
        self.assertEqual(self.keys({"problems": [engine.DISK_FULL]}), [])     # it ran full elsewhere
        # a nearly full disk under other fixes: room for them first (1 GiB wanted, 512 MiB enough to go on)
        self.patch(fixes.space, "free", lambda path="/": 300 * 2 ** 20)
        plan = fixes.plan({"boot": [{"kind": "unmounted", "target": "/boot", "text": "t"}]})
        self.assertEqual([(f["key"], f["args"]) for f in plan],
                         [("space", [str(2 ** 30), str(fixes.space.LOW)]), ("mount", ["/boot"])])

    def test_the_space_fix_by_hand_is_a_shell_line(self):
        """What arch-update-auto prints to run by hand: commands only, snapper's cleanup when there is snapper."""
        self.patch(fixes.space, "free", lambda path="/": 10 ** 9)
        res = {"space": {"need": 3 * 10 ** 9, "free": 10 ** 9}}
        self.patch(fixes.space.shutil, "which", lambda name: None)
        self.assertEqual(fixes.plan(res)[0]["how"], "sudo pacman -Sc; sudo journalctl --vacuum-size=200M")
        self.patch(fixes.space.shutil, "which", lambda name: "/usr/bin/snapper" if name == "snapper" else None)
        self.patch(engine, "elevation_command", lambda: "doas")
        self.assertEqual(fixes.plan(res)[0]["how"], "doas pacman -Sc; doas journalctl --vacuum-size=200M; "
                                                    "doas snapper -c root cleanup number")

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
        self.assertEqual([f["id"] for f in plan], ["mount", "dkms@linux", "initramfs@linux", "sbctl"])
        self.assertIn("NVIDIA", plan[1]["label"])

    def test_no_initramfs_fix_without_mkinitcpio_presets(self):
        os.remove(os.path.join(self.preset_dir, "linux.preset"))
        self.assertEqual(self.keys({"boot": [{"kind": "image", "path": "/boot/initramfs-linux.img", "text": "t"}]}),
                         [])

    def test_an_initramfs_fix_for_each_kernel_that_needs_one(self):
        """Only the kernels the problems name: one that cannot be built (linux-lts without its NVIDIA module) does
        not keep another one's broken image as it is."""
        self.two_kernels()
        image = {"kind": "image", "pkg": "linux", "path": "/boot/initramfs-linux.img", "how": "broken", "text": "t"}
        plan = fixes.plan({"boot": [image]})
        self.assertEqual([(f["id"], f["args"], f["argv"]) for f in plan],
                         [("initramfs@linux", ["p:linux", f"i:{KVER}:1:/boot/initramfs-linux.img"],
                           ["sudo", "mkinitcpio", "-p", "linux"])])
        # mkinitcpio failed in the update (it does not say for which kernel), or an image of no known kernel: all
        both = ["initramfs@linux", "initramfs@linux-lts"]
        self.assertEqual(self.ids({"boot": [{"kind": "initcpio", "text": "t"}]}), both)
        self.assertEqual(self.ids({"boot": [dict(image, pkg=None)]}), both)
        lts = fixes.plan({"boot": [dict(image, pkg="linux-lts")]})[0]
        self.assertEqual(lts["args"], ["p:linux-lts", f"i:{LTS}:1:/boot/initramfs-linux-lts.img",
                                       f"i:{LTS}:1:/boot/initramfs-linux-lts-fallback.img"])
        self.assertEqual(self.ids({"boot": [dict(image, pkg="linux-zen")]}), [])    # no preset of it here

    def test_no_initramfs_fix_for_a_kernel_the_kernel_fix_reinstalls(self):
        """pacman's own hook builds its images, once its kernel file is back."""
        self.two_kernels()
        image = {"kind": "image", "pkg": "linux", "path": "/boot/initramfs-linux.img", "how": "broken", "text": "t"}
        kernel = {"kind": "kernel", "pkg": "linux", "path": "/boot/vmlinuz-linux", "how": "missing", "text": "t"}
        plan = fixes.plan({"boot": [kernel, image]})
        self.assertEqual([f["id"] for f in plan], ["kernel@linux"])
        self.assertEqual(plan[0]["args"], ["r:linux", f"g:{engine.PACMAN_LOG}", "k:/boot/vmlinuz-linux",
                                           f"i:{KVER}:1:/boot/initramfs-linux.img"])
        self.assertEqual(plan[0]["label"], "переустановить ядро linux (вернёт файл ядра и initramfs)")
        self.assertEqual(self.ids({"boot": [kernel, image, dict(image, pkg="linux-lts")]}),
                         ["kernel@linux", "initramfs@linux-lts"])

    def test_depmod_and_dkms_rebuild_the_images_of_their_kernels(self):
        self.two_kernels()
        self.assertEqual(self.ids({"boot": [{"kind": "depmod", "pkg": "linux-lts", "kver": LTS, "text": "t"}]}),
                         ["depmod", "initramfs@linux-lts"])
        dkms = {"kind": "dkms", "module": "nvidia", "kvers": [KVER, LTS], "text": "t"}
        plan = fixes.plan({"boot": [dkms]})
        self.assertEqual([f["id"] for f in plan], ["dkms@linux", "dkms@linux-lts", "initramfs@linux",
                                                   "initramfs@linux-lts"])
        self.assertEqual([f["label"] for f in plan[:2]], [f"собрать драйвер NVIDIA под ядро {KVER}",
                                                          f"собрать драйвер NVIDIA под ядро {LTS}"])
        self.assertEqual(plan[1]["args"], [f"nvidia:{LTS}"])
        # a kernel version no installed package has: a fix for the whole machine, and no initramfs for it
        self.assertEqual(self.ids({"boot": [dict(dkms, kvers=["6.1.0-old"])]}), ["dkms"])

    def test_one_kernel_fix_per_kernel(self):
        self.two_kernels()
        pkg = os.path.join(self.t, "linux-7.2.14.arch1-1-x86_64.pkg.tar.zst")
        write(pkg, "", 0o644)
        self.patch(engine, "cached_package", lambda p, v: pkg if p == "linux" else None)
        issues = [{"kind": "kernel", "pkg": p, "path": f"/boot/vmlinuz-{p}", "how": "missing", "text": "t"}
                  for p in ("linux", "linux-lts")]
        plan = fixes.plan({"boot": issues})
        self.assertEqual([(f["id"], f["argv"]) for f in plan],
                         [("kernel@linux", ["sudo", "pacman", "-U", pkg]),
                          ("kernel@linux-lts", ["sudo", "pacman", "-S", "linux-lts"])])
        self.assertEqual(plan[0]["label"], "переустановить ядро linux из кэша pacman (вернёт файл ядра и initramfs)")
        self.assertEqual(plan[1]["args"][:3], ["r:linux-lts", f"g:{engine.PACMAN_LOG}", "k:/boot/vmlinuz-linux-lts"])
        # a newer linux-lts waits in the repository: that one only by a whole update, linux still from the cache
        self.patch(engine, "run", lambda cmd, **k: "linux-lts 6.18.60-1 -> 6.18.61-1" if cmd[-1] == "linux-lts"
                   else "")
        self.assertEqual(self.ids({"boot": issues}), ["kernel@linux"])

    def test_one_fix_for_all_unmounted_partitions_and_all_unsigned_files(self):
        plan = fixes.plan({"boot": [{"kind": "unmounted", "target": t, "text": "t"} for t in ("/boot", "/efi")]
                           + [{"kind": "unsigned", "files": ["/boot/a.efi", "/boot/b.efi", "/boot/c.efi"],
                               "text": "t"}]})
        self.assertEqual([(f["id"], f["args"]) for f in plan],
                         [("mount", ["/boot", "/efi"]), ("sbctl", ["/boot/a.efi", "/boot/b.efi", "/boot/c.efi"])])
        self.assertEqual((plan[0]["label"], plan[0]["how"]), ("смонтировать /boot и /efi",
                                                              "sudo mount /boot && sudo mount /efi"))
        self.assertEqual(plan[1]["label"], "подписать /boot/a.efi, /boot/b.efi и ещё 1 для Secure Boot")
        self.assertEqual(plan[1]["how"], "sudo sbctl sign -s /boot/a.efi && sudo sbctl sign -s /boot/b.efi && "
                                         "sudo sbctl sign -s /boot/c.efi")

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

    def test_an_id_is_planned_once(self):
        said = []
        self.patch(engine, "LOG", types.SimpleNamespace(event=lambda kind, text: said.append((kind, text))))
        twice = [fixes.fix("mount", "смонтировать /boot", ["sudo", "mount", "/boot"], ["/boot"]),
                 fixes.fix("mount", "смонтировать /efi", ["sudo", "mount", "/efi"], ["/efi"])]
        self.patch(fixes.mount, "plan", lambda ctx: twice)
        plan = fixes.plan({"boot": [{"kind": "unmounted", "target": "/boot", "text": "t"}]})
        self.assertEqual([(f["id"], f["args"]) for f in plan], [("mount", ["/boot"])])
        self.assertEqual([kind for kind, _ in said], ["ОШИБКА"])

    def test_ids_known_and_labels(self):
        one = fixes.fix("initramfs", "собрать заново initramfs ядра linux", ["sudo"], ["p:linux"], scope="linux")
        self.assertEqual((one["id"], fixes.scope_of(one["id"])), ("initramfs@linux", "linux"))
        self.assertEqual(fixes.scope_of("initramfs@lib32-linux@x"), "lib32-linux@x")   # split at the first @
        self.assertIsNone(fixes.scope_of("depmod"))
        old = {"key": "depmod", "label": "собрать список модулей", "argv": ["sudo", "depmod"], "args": ["7.2"]}
        fixes_in = [one, old, dict(one, label="второй раз"), dict(one, id="initramfs@"),
                    dict(one, id="kernel@linux"), dict(one, id="initramfs@bad name"), dict(one, id=None),
                    dict(old, id="depmod@x", args=["--"])]
        known = fixes.known(fixes_in)
        self.assertEqual([(f["id"], f["label"]) for f in known],
                         [("initramfs@linux", "собрать заново initramfs ядра linux"),
                          ("depmod", "собрать список модулей")])        # one saved without an id: its key
        self.assertNotIn("id", old)                                    # the saved one is not changed
        self.assertEqual(fixes.labels(fixes_in), {"initramfs@linux": "собрать заново initramfs ядра linux",
                                                  "depmod": "собрать список модулей"})
        argv = fixes.command(fixes_in)
        self.assertEqual(argv[7:], ["initramfs@linux", "p:linux", "--", "depmod", "7.2", "--"])

    def test_results_as_the_runner_writes_them(self):
        path = os.path.join(self.t, "result")
        write(path, "initramfs@linux ok\nmount refused pacman\n"
                    "initramfs@linux-lts rolledback full:/boot full:/boot full:relative full:/a/../b\n"
                    "kernel@linux rollbackfailed /tmp/arch-update-fix.AbCd1234 full:/efi\n"
                    "weird nosuchstate\nlonely\n\nspace notenough some-word-of-a-later-version\n", 0o600)
        results = fixes.read_results(path)
        self.assertEqual(results, [("initramfs@linux", "ok"), ("mount", "refused"), ("initramfs@linux-lts",
                                   "rolledback"), ("kernel@linux", "rollbackfailed"), ("space", "notenough")])
        self.assertEqual(dict(results)["mount"], "refused")
        self.assertEqual(results.extra["mount"], ["pacman"])
        self.assertEqual(results.kept, "/tmp/arch-update-fix.AbCd1234")
        self.assertEqual(results.full, ["/boot", "/efi"])
        self.assertFalse(os.path.exists(path))
        for folder in ("/tmp/arch-update-fix.short", "/tmp/arch-update-fix.AbCd1234/../../etc", "/etc"):
            write(path, f"kernel@linux rollbackfailed {folder}\n", 0o600)
            self.assertIsNone(fixes.read_results(path).kept, folder)
        missing = fixes.read_results(os.path.join(self.t, "none"))
        self.assertEqual((missing, missing.kept, missing.full, bool(missing)), ([], None, [], False))

    def plan_of_three(self):
        return [fixes.fix("initramfs", "собрать заново initramfs ядра linux (mkinitcpio)", ["sudo"], [], scope="linux"),
                fixes.fix("kernel", "переустановить ядро linux-lts (вернёт файл ядра и initramfs)", ["sudo"], [],
                          scope="linux-lts"),
                fixes.fix("mount", "смонтировать /boot", ["sudo", "mount", "/boot"], ["/boot"])]

    def test_outcome_and_journal_texts(self):
        plan = [fixes.fix("initramfs", "собрать initramfs заново", ["sudo", "mkinitcpio", "-P"], [])]
        self.assertEqual(fixes.outcome(0, [("initramfs", "ok")], plan)[0], "ok")
        kind, text = fixes.outcome(80, [("initramfs", "rolledback")], plan)
        self.assertEqual(kind, "rolledback")
        self.assertIn("вернул всё как было", text)
        self.assertEqual(fixes.outcome(81, [("initramfs", "rollbackfailed")], plan)[0], "rollbackfailed")
        self.assertEqual(fixes.outcome(1, [], plan)[0], "nothing")
        self.assertEqual(fixes.outcome(84, [("space", "notenough")])[0], "notenough")
        # no file for the results (a full /home): the exit status tells
        self.assertEqual(fixes.outcome(0, None, plan)[0], "ok")
        self.assertEqual(fixes.outcome(80, None, plan)[0], "rolledback")
        self.assertEqual(fixes.outcome(1, None, plan)[0], "nothing")
        # room for the fixes, not yet for the update; one done, the next could not even begin
        two = [fixes.fix("space", "освободить место", ["sudo", "pacman", "-Sc"], ["1", "1"])] + plan
        self.assertEqual(fixes.outcome(0, [("space", "partial"), ("initramfs", "ok")], two),
                         ("ok", "исправлено и проверено; для обновления места ещё мало"))
        kind, text = fixes.outcome(82, [("space", "ok"), ("initramfs", "nobackup")], two)
        self.assertEqual(kind, "stopped")
        self.assertIn("сделано: освободить место", text)
        self.assertEqual(fixes.outcome(fixes.SUDO_REFUSED, [], plan)[0], "nothing")
        self.assertIn("собрать initramfs заново: не помогло", fixes.journal_text(80, [("initramfs", "rolledback")], plan))

    def test_what_came_of_it_in_a_line(self):
        plan = self.plan_of_three()
        mount, kernel, initramfs = "«смонтировать /boot»", "«переустановить ядро linux-lts (вернёт файл ядра и " \
                                   "initramfs)»", "«собрать заново initramfs ядра linux (mkinitcpio)»"
        cases = [
            (fixes.REFUSED, fixes.Results([("mount", "refused")], {"mount": ["pacman"]}),
             ("nothing", f"{mount} не начато: сейчас работает pacman — подожди, пока он закончит, и нажми F ещё раз")),
            (fixes.REFUSED, [("mount", "refused")], ("nothing", f"{mount} не начато: сейчас нельзя — ничего не менял")),
            (fixes.NO_BACKUP, [("mount", "nobackup")],
             ("nothing", f"{mount} не начато: не смог сохранить копии — ничего не менял")),
            (fixes.UNCHANGED, [("kernel@linux-lts", "unchanged")],
             ("nothing", f"{kernel}: pacman ничего не поставил — ничего не менял")),
            (fixes.UNCHANGED, [("kernel@linux-lts", "unchanged"), ("initramfs@linux", "ok")],
             ("stopped", "сделано: собрать заново initramfs ядра linux (mkinitcpio); "
                         f"{kernel}: pacman ничего не поставил — ничего не менял")),
            (fixes.INCOMPLETE, [("kernel@linux-lts", "incomplete")],
             ("incomplete", f"{kernel}: файл ядра на месте, но mkinitcpio сообщил об ошибках — initramfs может "
                            "быть неполным")),
            (fixes.ROLLED_BACK, fixes.Results([("initramfs@linux", "rolledback")], {"initramfs@linux": ["full:/boot"]}),
             ("rolledback", f"{initramfs} не помогло: на /boot не хватило места — вернул всё как было")),
            (fixes.ROLLBACK_FAILED, fixes.Results([("initramfs@linux", "rollbackfailed")],
                                                  {"initramfs@linux": ["/tmp/arch-update-fix.AbCd1234"]}),
             ("rollbackfailed", f"{initramfs} не помогло, и вернуть как было удалось не всё — копии в "
                                "/tmp/arch-update-fix.AbCd1234, НЕ перезагружайся")),
            (fixes.ROLLBACK_FAILED, fixes.Results([("initramfs@linux", "rollbackfailed")],
                                                  {"initramfs@linux": ["/etc"]}),
             ("rollbackfailed", f"{initramfs} не помогло, и вернуть как было удалось не всё — копии в /tmp, "
                                "НЕ перезагружайся")),
            (fixes.NOT_ENOUGH, [("space", "notenough"), ("mount", "skipped")],
             ("notenough", "места на диске всё ещё мало")),
            (fixes.SUDO_REFUSED, [], ("nothing", "пароль sudo не принят — ничего не менял")),
            (None, None, ("nothing", "не запустилось")),
            (fixes.INCOMPLETE, None, ("incomplete", f"{initramfs}: файл ядра на месте, но mkinitcpio сообщил об "
                                                    "ошибках — initramfs может быть неполным")),
        ]
        for status, results, want in cases:
            self.assertEqual(fixes.outcome(status, results, plan), want, (status, results))
            self.assertNotIn("место освободил", want[1])
            self.assertNotRegex(want[1], r"»: [^—]*: ")                 # «x»: не начал: … no more

    def test_the_journal_line(self):
        plan = self.plan_of_three()
        labels = "собрать заново initramfs ядра linux (mkinitcpio), переустановить ядро linux-lts (вернёт файл " \
                 "ядра и initramfs), смонтировать /boot"
        self.assertEqual(fixes.journal_text(125, [], plan), f"{labels}: не начато — пароль sudo не принят (код 125)")
        self.assertEqual(fixes.journal_text(1, [], plan),
                         f"{labels}: не начато (код 1: пароль не принят или отменён?)")
        self.assertEqual(fixes.journal_text(None, None, plan), f"{labels}: не запустилось")
        self.assertEqual(fixes.journal_text(1, [], []), "ничего не сделано (код 1)")
        self.assertEqual(fixes.journal_text(fixes.REFUSED, fixes.Results([("mount", "refused")], {"mount": ["pacman"]}),
                                            plan), "смонтировать /boot: не начато — работал pacman, ничего не менял "
                                                   "(код 83)")
        line = fixes.journal_text(81, fixes.Results(
            [("initramfs@linux", "rollbackfailed"), ("kernel@linux-lts", "skipped")],
            {"initramfs@linux": ["/tmp/arch-update-fix.AbCd1234", "full:/boot"]}), plan)
        self.assertEqual(line, "собрать заново initramfs ядра linux (mkinitcpio): не помогло, и вернуть как было "
                               "удалось не всё — копии в /tmp/arch-update-fix.AbCd1234; переустановить ядро linux-lts "
                               "(вернёт файл ядра и initramfs): не делал (не прошло исправление перед ним) (код 81)")
        self.assertIn("собрать заново initramfs ядра linux (mkinitcpio): не помогло: на /boot не хватило места — "
                      "вернул всё как было", fixes.journal_text(80, fixes.Results(
                          [("initramfs@linux", "rolledback")], {"initramfs@linux": ["full:/boot"]}), plan))
        self.assertIn("переустановить ядро linux-lts (вернёт файл ядра и initramfs): pacman ничего не поставил",
                      fixes.journal_text(86, [("kernel@linux-lts", "unchanged")], plan))
        # no file for the results: what the runner's own status says
        self.assertEqual(fixes.journal_text(0, None, plan).count("исправлено и проверено"), 3)

    def test_what_was_done(self):
        plan = self.plan_of_three()
        results = [("mount", "ok"), ("kernel@linux-lts", "rolledback"), ("initramfs@linux", "partial")]
        self.assertEqual(fixes.done(results, plan), ["смонтировать /boot",
                                                     "собрать заново initramfs ядра linux (mkinitcpio)"])
        self.assertEqual(fixes.done(None, plan), [])
        self.assertEqual(fixes.done([("depmod", "ok")]), ["depmod"])       # not in the plan: its id


if __name__ == "__main__":
    unittest.main(verbosity=1)
