"""«Переустановить ядро», one fix per kernel: its kernel file missing or empty. The installed kernel package again,
that very version: from pacman's cache when the file is there (pacman -U), else from the repository (pacman -S) but
only while the repository has no newer one (that would update the kernel alone, without its headers and the rest:
a partial upgrade; then there is no fix for it here and the problem's text says what to do). pacman asks before it
does anything; its hooks copy the kernel, build its initramfs and DKMS modules and sign.

Kept first: that kernel's boot files (kernel, initramfs, UKI); put back if pacman fails or a file is not right
after it (the kernel file there, every initramfs built for it with its modules). What the transaction did is read
in pacman's own log, only what it wrote while this fix ran ("[ALPM] transaction started", its hooks' output as
"[ALPM-SCRIPTLET] …"), and in what pacman said on stderr ("error: command failed to execute correctly" when a hook
failed: pacman 7 writes that into no log, and still exits 0):
  no "transaction started" and pacman failed   nothing was installed: the question answered no, a download that
                                               failed ("unchanged": nothing to put back, nothing was changed)
  pacman went through, but mkinitcpio (in its hook) reported errors, or another hook failed (sbctl's signature…)
                                               "incomplete": the kernel file is back, but it may not boot; not put
                                               back (that would take the kernel file away again); "hook" noted when
                                               it was not mkinitcpio"""

import os

import engine
from fixes import NAME, fix

KEY = "kernel"


def plan(ctx):
    pkgs = sorted({i.get("pkg") for i in ctx.of("kernel") if isinstance(i.get("pkg"), str) and NAME.fullmatch(i["pkg"])})
    if not pkgs:
        return []
    installed = engine.installed_versions()
    out = []
    for p in pkgs:
        cached = engine.cached_package(p, installed[p]) if p in installed else None
        specs = [f"g:{engine.PACMAN_LOG}", *ctx.image_specs({p})]
        if cached and os.path.isfile(cached):
            out.append(fix(KEY, f"переустановить ядро {p} из кэша pacman (вернёт файл ядра и initramfs)",
                           [ctx.su, "pacman", "-U", cached], [f"c:{cached}", *specs], scope=p))
        elif not engine.run(["pacman", "-Qu", p], timeout=20).strip():     # a newer one waits: only a whole -Syu
            out.append(fix(KEY, f"переустановить ядро {p} (вернёт файл ядра и initramfs)",
                           [ctx.su, "pacman", "-S", p], [f"r:{p}", *specs], scope=p))
    return out


SCRIPT = r"""
# kernel c:<cached package file>… r:<package>… g:<pacman's log> k:<kernel file>… i:<image spec>… u:<UKI>…
kernel_files() {
    local a
    for a; do
        case $a in
            k:*) printf '%s\n' "${a#k:}" ;;
            u:*) printf '%s\n%s\n' "${a#u:}" "${a#u:}.tmp" ;;          # and mkinitcpio's leftover, see initramfs
            i:*) printf '%s\n%s\n' "${a#i:*:*:}" "${a#i:*:*:}.tmp" ;;
        esac
    done
}
kernel_backup() {
    local files=()
    mapfile -t files < <(kernel_files "$@")
    room_for "${files[@]}" && keep "${files[@]}" && : >"$work/pacman.err"
}
# kernel_pacman <pacman's arguments…>: pacman as in the terminal (its output and questions there), what it says on
# stderr also into $work/pacman.err; its exit status
kernel_pacman() {
    local rc
    { pacman "$@" 2>&1 1>&4 4>&- | tee -a "$work/pacman.err" >&2; rc=${PIPESTATUS[0]}; } 4>&1
    return "$rc"
}
# kernel_back <args…>: every kernel file there again
kernel_back() {
    local a
    for a; do
        case $a in k:*) [ -s "${a#k:}" ] || return 1 ;; esac
    done
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
    if [ "$rc" != 0 ]; then
        # it did not even begin (the question answered no, a download failed): nothing to put back
        if ! grep -q '\] transaction started' <<<"$new" && kept_same; then
            warn "pacman ничего не поставил — ничего не менял"
            failed_as=unchanged
        fi
        return 1
    fi
    if grep -qiE "$INITCPIO_TROUBLE" <<<"$new"; then
        # the kernel file is back: putting the files back would take it away again
        if kernel_back "$@"; then
            bad "файл ядра на месте, но mkinitcpio сообщил об ошибках — initramfs может быть неполным"
            bad "НЕ перезагружайся, пока initramfs не собран без ошибок"
            failed_as=incomplete
        fi
        return 1
    fi
    if grep -qF 'command failed to execute correctly' "$work/pacman.err" 2>/dev/null \
            || grep -qF 'command failed to execute correctly' <<<"$new"; then
        if kernel_back "$@"; then
            bad "файл ядра на месте, но один из хуков pacman закончился с ошибкой (выше) — загрузка может не пройти"
            bad "НЕ перезагружайся, пока это не исправлено"
            failed_as=incomplete
            note_extra=hook
        fi
        return 1
    fi
}
kernel_verify() {
    local a ok=0
    for a; do
        case $a in
            k:*|u:*) nonempty "${a#?:}" || ok=1 ;;
            i:*) image_ok "$a" || ok=1 ;;
        esac
    done
    return "$ok"
}
"""
