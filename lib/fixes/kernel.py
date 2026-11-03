"""«Переустановить ядро»: a kernel file missing or empty. The installed kernel package again, that very
version: from pacman's cache when the file is there (pacman -U), else from the repository (pacman -S) but only
while the repository has no newer one (that would update the kernel alone, without its headers and the rest: a
partial upgrade; then there is no fix here and the problem's text says what to do). pacman asks before it does
anything; its hooks copy the kernel, build its initramfs and DKMS modules and sign.

Kept first: that kernel's boot files (kernel, initramfs, UKI); put back if pacman fails or a file is not right
after it (the kernel file there, every initramfs built for it with its modules)."""

import os

import engine
from fixes import NAME, fix

KEY = "kernel"


def plan(ctx):
    pkgs = sorted({i.get("pkg") for i in ctx.of("kernel") if isinstance(i.get("pkg"), str) and NAME.fullmatch(i["pkg"])})
    if not pkgs:
        return []
    installed = engine.installed_versions()
    cached = {p: engine.cached_package(p, installed[p]) for p in pkgs if p in installed}
    cached = {p: f for p, f in cached.items() if f and os.path.isfile(f)}
    rest = [p for p in pkgs if p not in cached]
    if any(engine.run(["pacman", "-Qu", p], timeout=20).strip() for p in rest):
        return []                               # a newer kernel waits: only a whole -Syu may bring it
    specs = ctx.image_specs(set(pkgs))
    out = []
    if cached:
        out.append(fix(KEY, f"переустановить ядро ({', '.join(cached)}) из кэша pacman: вернёт файл ядра и initramfs",
                       [ctx.su, "pacman", "-U", *cached.values()], [f"c:{f}" for f in cached.values()] + specs))
    if rest:
        out.append(fix(KEY, f"переустановить ядро ({', '.join(rest)}): вернёт файл ядра и initramfs",
                       [ctx.su, "pacman", "-S", *rest], [f"r:{p}" for p in rest] + specs))
    return out


SCRIPT = r"""
# kernel c:<cached package file>… r:<package>… k:<kernel file>… i:<image spec>… u:<unified kernel image>…
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
    room_for "${files[@]}" && keep "${files[@]}"
}
kernel_apply() {
    local a cache=() repo=()
    for a; do
        case $a in
            c:/*.pkg.tar*) cache+=("${a#c:}") ;;
            r:*) repo+=("${a#r:}") ;;
        esac
    done
    if [ ${#cache[@]} -gt 0 ]; then
        step "Ставлю ядро заново из кэша pacman (та же версия)"
        pacman -U "${cache[@]}" || return 1
    fi
    if [ ${#repo[@]} -gt 0 ]; then
        step "Ставлю ядро заново (та же версия)"
        pacman -S "${repo[@]}" || return 1
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
