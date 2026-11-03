"""«Переустановить ядро», one fix per kernel: its kernel file missing or empty. The installed kernel package again,
that very version: from pacman's cache when the file is there (pacman -U), else from the repository (pacman -S) but
only while the repository has no newer one (that would update the kernel alone, without its headers and the rest:
a partial upgrade; then there is no fix for it here and the problem's text says what to do). pacman asks before it
does anything; its hooks copy the kernel, build its initramfs and DKMS modules and sign.

Kept first: that kernel's boot files (kernel, initramfs, UKI), and mkinitcpio's "<image>.tmp" as missing whatever is
there (a stale one, left by a build that failed before, is removed before pacman, said: its room is what the hook's
build needs); put back if pacman fails or a file is not right after it: the kernel file there and whole (not shorter
than the package's own kernel, /usr/lib/modules/<version>/vmlinuz, planned as v: when the package has one: the hook
copies that file as it is and sbctl's signature only adds to it, so never compared byte for byte; a copy a full
/boot cut short is no kernel), every initramfs built for it with its modules. What the transaction did is read in
pacman's own log, only what it wrote while this fix ran ("[ALPM] transaction started", its hooks' output as
"[ALPM-SCRIPTLET] …"), and in what pacman said on stderr ("error: command failed to execute correctly" when a hook
failed: pacman 7 writes that into no log, and still exits 0):
  no "transaction started" and pacman failed   nothing was installed: the question answered no, a download that
                                               failed ("unchanged": nothing to put back, nothing was changed)
  pacman went through, but mkinitcpio (in its hook) reported errors, or another hook failed (sbctl's signature…)
                                               "incomplete" when the kernel file is back whole: it may not boot; not
                                               put back (that would take the kernel file away again), but what
                                               mkinitcpio left half written ("<image>.tmp") is removed; "hook" noted
                                               when it was not mkinitcpio. A kernel file cut short is a failed fix:
                                               all put back, the kernel file missing again.
When a file system ran out of room ("No space left on device" in what pacman and its hooks said), it says which one
and notes it (ran_full, looked at before anything gives the room back): the UI then names /boot, not pacman's
cache."""

import os

import engine
from fixes import NAME, fix, path_ok

KEY = "kernel"


def plan(ctx):
    pkgs = sorted({i.get("pkg") for i in ctx.of("kernel") if isinstance(i.get("pkg"), str) and NAME.fullmatch(i["pkg"])})
    if not pkgs:
        return []
    installed = engine.installed_versions()
    out = []
    for p in pkgs:
        cached = engine.cached_package(p, installed[p]) if p in installed else None
        specs = [f"g:{engine.PACMAN_LOG}", *own_kernel(ctx, p), *ctx.image_specs({p})]
        if cached and os.path.isfile(cached):
            out.append(fix(KEY, f"переустановить ядро {p} из кэша pacman (вернёт файл ядра и initramfs)",
                           [ctx.su, "pacman", "-U", cached], [f"c:{cached}", *specs], scope=p))
        elif not engine.run(["pacman", "-Qu", p], timeout=20).strip():     # a newer one waits: only a whole -Syu
            out.append(fix(KEY, f"переустановить ядро {p} (вернёт файл ядра и initramfs)",
                           [ctx.su, "pacman", "-S", p], [f"r:{p}", *specs], scope=p))
    return out


def own_kernel(ctx, pkg):
    """[v:<the package's own kernel>] when it has one in its modules folder (every Arch kernel package: what pacman's
    hook copies to /boot), [] otherwise (a package that puts its kernel into /boot itself: nothing to measure by)."""
    kver = ctx.kernels().get(pkg)
    own = os.path.join(engine.MODULES_DIR, kver, "vmlinuz") if kver else None
    return [f"v:{own}"] if own and path_ok(own) and os.path.isfile(own) else []


SCRIPT = r"""
# kernel c:<cached package file>… r:<package>… g:<pacman's log> v:<the package's own kernel> k:<kernel file>…
#        i:<image spec>… u:<UKI>…
# kernel_files <args…>: that kernel's boot files, one a line
kernel_files() {
    local a
    for a; do
        case $a in
            k:*) printf '%s\n' "${a#k:}" ;;
            u:*) printf '%s\n' "${a#u:}" ;;
            i:*) printf '%s\n' "${a#i:*:*:}" ;;
        esac
    done
}
kernel_backup() {
    local files=() tmps=()
    mapfile -t files < <(kernel_files "$@")
    mapfile -t tmps < <(tmps_of "$@")
    room_for "${files[@]}" && keep "${files[@]}" && keep_missing "${tmps[@]}" && : >"$work/pacman.err"
}
# kernel_pacman <pacman's arguments…>: pacman as in the terminal (its output and questions there), what it says on
# stderr also into $work/pacman.err; its exit status
kernel_pacman() {
    local rc
    { pacman "$@" 2>&1 1>&4 4>&- | tee -a "$work/pacman.err" >&2; rc=${PIPESTATUS[0]}; } 4>&1
    return "$rc"
}
# kernel_trouble <kernel file> <args…>: what is wrong with that kernel file, in a line (status 1); nothing when it is
# there whole: not empty, and not shorter than the package's own kernel (v:, when it is there). pacman's hook copies
# that file as it is and sbctl signs it in place, which only adds to it: a signed kernel differs from the package's,
# so it is never compared byte for byte. A copy cut short (a full /boot) still starts as a kernel, and does not boot.
kernel_trouble() {
    local f=$1 a v='' want have
    shift
    if [ ! -s "$f" ]; then printf '%s нет или он пустой\n' "$f"; return 1; fi
    for a; do case $a in v:/*) v=${a#v:} ;; esac; done
    if [ -z "$v" ] || [ ! -f "$v" ]; then return 0; fi
    want=$(stat -c %s -- "$v" 2>/dev/null) || return 0
    have=$(stat -c %s -- "$f" 2>/dev/null) || have=0
    if [ "$have" -lt "$want" ]; then
        printf '%s обрезан: %s из %s\n' "$f" "$(size "$have")" "$(size "$want")"
        return 1
    fi
}
# kernel_back <args…>: every kernel file there again, and whole (what is not, said)
kernel_back() {
    local a why ok=0
    for a; do
        case $a in
            k:*) if ! why=$(kernel_trouble "${a#k:}" "$@"); then bad "$why"; ok=1; fi ;;
        esac
    done
    return "$ok"
}
kernel_apply() {
    local a cache=() repo=() log='' from=0 rc=0 new=''
    for a; do
        case $a in
            c:/*.pkg.tar*) cache+=("${a#c:}") ;;
            r:*) repo+=("${a#r:}") ;;
            g:/*) log=${a#g:} ;;
        esac
    done
    # what a build that failed before left on /boot (kept as missing): its room is what pacman's hook needs
    drop_tmps "$@" || return 1
    # what pacman writes into its log from now on: whether the transaction began, what its hooks said
    if [ -n "$log" ] && [ -f "$log" ]; then from=$(stat -c %s -- "$log" 2>/dev/null) || from=0; fi
    if [ ${#cache[@]} -gt 0 ]; then
        step "Ставлю ядро заново из кэша pacman (та же версия)"
        kernel_pacman -U "${cache[@]}" || rc=1
    fi
    if [ "$rc" = 0 ] && [ ${#repo[@]} -gt 0 ]; then
        step "Ставлю ядро заново (та же версия)"
        kernel_pacman -S "${repo[@]}" || rc=1
    fi
    if [ -n "$log" ] && [ -f "$log" ]; then new=$(tail -c +"$((from + 1))" -- "$log" 2>/dev/null); fi
    # a file system that ran out of room: which one, looked at now, before a rollback or the leftovers below give
    # the room back
    { printf '%s\n' "$new"; cat -- "$work/pacman.err" 2>/dev/null; } >"$work/pacman.said"
    if grep -qi 'No space left on device' "$work/pacman.said"; then ran_full '' "$work/pacman.said" "$@"; fi
    if [ "$rc" != 0 ]; then
        # it did not even begin (the question answered no, a download failed): nothing to put back
        if ! grep -q '\] transaction started' <<<"$new" && kept_same; then
            warn "pacman ничего не поставил — ничего не менял"
            failed_as=unchanged
        fi
        return 1
    fi
    if grep -qiE "$INITCPIO_TROUBLE" <<<"$new"; then
        # the kernel file is back: putting the files back would take it away again; but not one cut short
        if kernel_back "$@"; then
            bad "файл ядра на месте, но mkinitcpio сообщил об ошибках — initramfs может быть неполным"
            bad "НЕ перезагружайся, пока initramfs не собран без ошибок"
            drop_tmps "$@" || bad "не смог убрать то, что оставил mkinitcpio"
            failed_as=incomplete
        fi
        return 1
    fi
    if grep -qF 'command failed to execute correctly' "$work/pacman.err" 2>/dev/null \
            || grep -qF 'command failed to execute correctly' <<<"$new"; then
        if kernel_back "$@"; then
            bad "файл ядра на месте, но один из хуков pacman закончился с ошибкой (выше) — загрузка может не пройти"
            bad "НЕ перезагружайся, пока это не исправлено"
            drop_tmps "$@" || bad "не смог убрать то, что оставил mkinitcpio"
            failed_as=incomplete
            note_extra="${note_extra:+$note_extra }hook"
        fi
        return 1
    fi
}
kernel_verify() {
    local a why ok=0
    for a; do
        case $a in
            k:*) if why=$(kernel_trouble "${a#k:}" "$@"); then good "${a#k:} на месте"; else bad "$why"; ok=1; fi ;;
            u:*) nonempty "${a#u:}" || ok=1 ;;
            i:*) image_ok "$a" || ok=1 ;;
        esac
    done
    return "$ok"
}
"""
