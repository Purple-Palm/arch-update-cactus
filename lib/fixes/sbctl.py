"""«Подписать для Secure Boot»: a boot file sbctl says is not signed (the firmware would refuse it). sbctl sign
-s <file> (signed, and kept in sbctl's list so its pacman hook signs it again on the next update). Kept first:
the file and sbctl's list of files; put back if sbctl fails or still calls the file unsigned."""

from fixes import path_ok, fix

KEY = "sbctl"
DBS = ("/var/lib/sbctl/files.json", "/usr/share/secureboot/files.json")


def plan(ctx):
    files = [f for f in dict.fromkeys(f for i in ctx.of("unsigned") for f in i.get("files") or []) if path_ok(f)]
    return [fix(KEY, f"подписать для Secure Boot: {f}", [ctx.su, "sbctl", "sign", "-s", f], [f]) for f in files]


SCRIPT = r"""
# sbctl <file>…
sbctl_db() {
    local d
    for d in """ + " ".join(DBS) + r"""; do
        if [ -e "$d" ]; then printf '%s\n' "$d"; return; fi
    done
}
sbctl_backup() {
    local files=("$@") db
    db=$(sbctl_db)
    [ -n "$db" ] && files+=("$db")
    room_for "${files[@]}" && keep "${files[@]}"
}
sbctl_apply() {
    local f
    for f; do
        step "Подписываю для Secure Boot: $f"
        sbctl sign -s "$f" || return 1
    done
}
sbctl_verify() {
    local f out
    out=$(sbctl verify 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g')
    for f; do
        if grep -qF -- "$f is signed" <<<"$out"; then good "$f подписан"; else bad "$f так и не подписан"; return 1; fi
    done
}
"""
