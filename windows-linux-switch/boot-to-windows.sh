#!/usr/bin/env bash
#
# boot-to-windows.sh - reboot straight from NixOS into Windows, one time only,
#                      via the UEFI "BootNext" variable.
#
# Under the hood this is nothing more than:
#
#     efibootmgr --bootnext <number of the Windows entry>
#     systemctl reboot
#
# The firmware starts that entry exactly once. The permanent boot order (BootOrder)
# is never touched, so the reboot after that one goes back to the firmware's default.
# Works with any bootloader on the NixOS side (systemd-boot, GRUB, ...) because the
# bootloader is not involved at all - the firmware is told directly.
#
# Machine-specific values are NOT in this file. They live in a config file that is
# git-ignored (default: boot-to-windows.conf next to this script). Create it by copying
# boot-to-windows.example.conf and replacing the placeholders. Full manual: README.md.
#
# Usage:  boot-to-windows.sh [options]
#
#   -l, --list            Print the firmware boot entries (parsed "efibootmgr" output) and exit.
#   -n, --dry-run         Resolve the target entry and print what would be executed; change nothing.
#   -N, --no-reboot       Set BootNext but do not reboot (the next reboot/power-on boots Windows).
#   -d, --delay SECONDS   Seconds to wait before rebooting (overrides REBOOT_DELAY from the config).
#   -c, --config FILE     Config file to use (default: boot-to-windows.conf next to this script).
#   -h, --help            Show this help.
#
# Environment:
#   EFIBOOTMGR            Path of the efibootmgr binary to use (optional). Without it the script
#                         uses the one in PATH, or fetches it from nixpkgs on the fly.
#
# Requirements: booted in UEFI mode; efibootmgr; sudo or doas (only used to run efibootmgr).

set -euo pipefail

# ----------------------------------------------------------------------------
# Constants. Nothing machine-specific lives here - see the config file for that.
# ----------------------------------------------------------------------------
SCRIPT_TAG="boot-to-windows"
PLACEHOLDER="REPLACE_ME"
DEFAULT_CONFIG_NAME="boot-to-windows.conf"
EXAMPLE_CONFIG_NAME="boot-to-windows.example.conf"

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname -- "$SCRIPT_PATH")"

# Settings: built-in defaults, then the config file, then command-line options.
TARGET_MATCH=""
TARGET_BOOTNUM=""
REBOOT_DELAY=5

OPT_LIST=0
OPT_DRY_RUN=0
OPT_NO_REBOOT=0
OPT_DELAY=""
OPT_CONFIG=""

# ----------------------------------------------------------------------------
# Output helpers. Messages go to stderr; only --list data goes to stdout.
# ----------------------------------------------------------------------------
info() { printf '[%s] %s\n' "$SCRIPT_TAG" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$SCRIPT_TAG" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$SCRIPT_TAG" "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE_EOF'
Usage: boot-to-windows.sh [options]

Reboot straight from NixOS into Windows, one time only (UEFI BootNext).

  -l, --list            Print the firmware boot entries and exit (nothing is changed).
  -n, --dry-run         Resolve the target entry and print what would be executed.
  -N, --no-reboot       Set BootNext but do not reboot.
  -d, --delay SECONDS   Seconds to wait before rebooting (overrides REBOOT_DELAY).
  -c, --config FILE     Config file (default: boot-to-windows.conf next to the script).
  -h, --help            Show this help.

Config file: copy boot-to-windows.example.conf to boot-to-windows.conf and replace the
placeholders. The real config file is git-ignored. Full manual: README.md.
USAGE_EOF
}

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -l|--list)      OPT_LIST=1 ;;
        -n|--dry-run)   OPT_DRY_RUN=1 ;;
        -N|--no-reboot) OPT_NO_REBOOT=1 ;;
        -d|--delay)     [[ $# -ge 2 ]] || die "option $1 needs a value"; OPT_DELAY="$2"; shift ;;
        --delay=*)      OPT_DELAY="${1#*=}" ;;
        -c|--config)    [[ $# -ge 2 ]] || die "option $1 needs a value"; OPT_CONFIG="$2"; shift ;;
        --config=*)     OPT_CONFIG="${1#*=}" ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

# ----------------------------------------------------------------------------
# Config file: plain KEY=value lines. It is parsed, never sourced, so a config
# file can not run code and odd characters in labels are harmless.
# ----------------------------------------------------------------------------
load_config() {
    local file="$1" line key val lineno=0
    local re_skip='^[[:space:]]*(#|$)'
    local re_kv='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$'

    [[ -f $file ]] || die "config file not found: $file
  Copy '$EXAMPLE_CONFIG_NAME' to '$(basename -- "$file")' (same folder as the script) and replace the placeholders.
  See README.md, section 'Configuration'."

    while IFS= read -r line || [[ -n $line ]]; do
        lineno=$((lineno + 1))
        line="${line%$'\r'}"                       # tolerate CRLF line endings
        [[ $line =~ $re_skip ]] && continue        # blank lines and comments
        if [[ $line =~ $re_kv ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            val="${val%"${val##*[![:space:]]}"}"   # trim trailing whitespace
            if [[ ${#val} -ge 2 ]] && { [[ $val == \"*\" ]] || [[ $val == \'*\' ]]; }; then
                val="${val:1:${#val}-2}"           # strip one pair of surrounding quotes
            fi
            case "$key" in
                TARGET_MATCH)   TARGET_MATCH="$val" ;;
                TARGET_BOOTNUM) TARGET_BOOTNUM="$val" ;;
                REBOOT_DELAY)   REBOOT_DELAY="$val" ;;
                *)              warn "$file:$lineno: unknown key '$key' is ignored" ;;
            esac
        else
            warn "$file:$lineno: cannot parse line (expected KEY=value): $line"
        fi
    done < "$file"
}

validate_settings() {
    local re_delay='^[0-9]+$'
    local re_bootnum='^[0-9A-Fa-f]{1,4}$'

    if [[ -n $OPT_DELAY ]]; then
        REBOOT_DELAY="$OPT_DELAY"
    fi
    [[ $REBOOT_DELAY =~ $re_delay ]] || die "REBOOT_DELAY must be a whole number of seconds (got '$REBOOT_DELAY')"

    if [[ $TARGET_BOOTNUM == "$PLACEHOLDER" ]]; then
        TARGET_BOOTNUM=""
    fi
    if [[ -n $TARGET_BOOTNUM ]]; then
        TARGET_BOOTNUM="${TARGET_BOOTNUM#[Bb][Oo][Oo][Tt]}"  # accept "Boot0001" as well as "0001"
        [[ $TARGET_BOOTNUM =~ $re_bootnum ]] || die "TARGET_BOOTNUM must be a hex entry number like 0001 (got '$TARGET_BOOTNUM')"
        printf -v TARGET_BOOTNUM '%04X' "0x$TARGET_BOOTNUM"
    elif [[ -z $TARGET_MATCH || $TARGET_MATCH == "$PLACEHOLDER" ]]; then
        die "the config file still contains placeholders: $CONFIG_FILE
  Run '$(basename -- "$SCRIPT_PATH") --list', find the entry that boots Windows and set TARGET_MATCH (or TARGET_BOOTNUM).
  See README.md, section 'Configuration'."
    fi
}

# ----------------------------------------------------------------------------
# Environment checks
# ----------------------------------------------------------------------------
[[ -d /sys/firmware/efi ]] || die "this system is not booted in UEFI mode (no /sys/firmware/efi), so UEFI BootNext is not available."
[[ -d /sys/firmware/efi/efivars ]] || die "efivarfs is not mounted at /sys/firmware/efi/efivars (try: mount -t efivarfs efivarfs /sys/firmware/efi/efivars)."

# SUDO is the command prefix used for efibootmgr (and as a fallback for the reboot).
SUDO=()
SUDO_DISPLAY=""
if [[ $EUID -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO=(sudo)
    elif command -v doas >/dev/null 2>&1; then
        SUDO=(doas)
    else
        die "not running as root and neither sudo nor doas is available."
    fi
    SUDO_DISPLAY="${SUDO[0]} "
fi

find_efibootmgr() {
    local candidate
    if candidate="$(command -v efibootmgr 2>/dev/null)"; then
        printf '%s\n' "$candidate"
        return 0
    fi
    # Not installed system-wide. Fetch it from nixpkgs without installing anything
    # (recommended fix: environment.systemPackages = [ pkgs.efibootmgr ]; in configuration.nix).
    if command -v nix >/dev/null 2>&1; then
        info "efibootmgr is not in PATH; fetching it from nixpkgs with 'nix build' (this can take a moment)..."
        if candidate="$(nix --extra-experimental-features 'nix-command flakes' build --no-link --print-out-paths 'nixpkgs#efibootmgr' 2>/dev/null)"; then
            candidate="${candidate%%$'\n'*}"
            if [[ -x "$candidate/bin/efibootmgr" ]]; then
                printf '%s\n' "$candidate/bin/efibootmgr"
                return 0
            fi
        fi
    fi
    if command -v nix-build >/dev/null 2>&1; then
        if candidate="$(nix-build --no-out-link '<nixpkgs>' -A efibootmgr 2>/dev/null)"; then
            candidate="${candidate%%$'\n'*}"
            if [[ -x "$candidate/bin/efibootmgr" ]]; then
                printf '%s\n' "$candidate/bin/efibootmgr"
                return 0
            fi
        fi
    fi
    return 1
}

if [[ -n ${EFIBOOTMGR:-} ]]; then
    [[ -x $EFIBOOTMGR ]] || die "EFIBOOTMGR=$EFIBOOTMGR is not an executable file."
else
    EFIBOOTMGR="$(find_efibootmgr)" || die "efibootmgr was not found. Add 'pkgs.efibootmgr' to environment.systemPackages, or set EFIBOOTMGR=/path/to/efibootmgr."
fi

# ----------------------------------------------------------------------------
# Firmware boot entries ("efibootmgr" output)
#
# Example output (the device path after the label, if any, is separated by a TAB):
#
#   BootCurrent: 0001
#   Timeout: 1 seconds
#   BootOrder: 0001,0000
#   Boot0000* Windows Boot Manager	HD(1,GPT,...)/File(\EFI\MICROSOFT\BOOT\BOOTMGFW.EFI)...
#   Boot0001* Linux Boot Manager	HD(1,GPT,...)/File(\EFI\systemd\systemd-bootx64.efi)
#
# "Boot0000*" = active entry, "Boot0000 " (space instead of *) = inactive entry.
# ----------------------------------------------------------------------------

# Prints the raw efibootmgr output. Reading usually works without root (efivarfs is
# world-readable); if it does not, retry through sudo/doas.
read_efibootmgr() {
    local out
    if out="$("$EFIBOOTMGR" 2>/dev/null)"; then
        printf '%s\n' "$out"
        return 0
    fi
    if [[ ${#SUDO[@]} -gt 0 ]]; then
        out="$("${SUDO[@]}" "$EFIBOOTMGR")" || return 1
        printf '%s\n' "$out"
        return 0
    fi
    return 1
}

# Converts raw efibootmgr output into one line per entry: NUM<TAB>active|inactive<TAB>LABEL
parse_entries() {
    local raw="$1" line num flag label
    local re=$'^Boot([0-9A-Fa-f]{4})([* ]) ?([^\t]*)'
    while IFS= read -r line; do
        [[ $line =~ $re ]] || continue
        num="${BASH_REMATCH[1]^^}"
        flag="${BASH_REMATCH[2]}"
        label="${BASH_REMATCH[3]}"
        label="${label#"${label%%[![:space:]]*}"}"   # trim leading whitespace
        label="${label%"${label##*[![:space:]]}"}"   # trim trailing whitespace
        if [[ $flag == '*' ]]; then flag=active; else flag=inactive; fi
        printf '%s\t%s\t%s\n' "$num" "$flag" "$label"
    done <<< "$raw"
}

# Prints parsed entries as a table.
print_entries() {
    local entries="$1" num flag label
    printf '%-9s %-9s %s\n' "ENTRY" "STATE" "LABEL"
    while IFS=$'\t' read -r num flag label; do
        [[ -n $num ]] || continue
        printf 'Boot%-5s %-9s %s\n' "$num" "$flag" "$label"
    done <<< "$entries"
}

# Prints the parsed entries that match TARGET_BOOTNUM (if set) or TARGET_MATCH.
select_targets() {
    local entries="$1" num flag label
    local -a hits=()
    while IFS=$'\t' read -r num flag label; do
        [[ -n $num ]] || continue
        if [[ -n $TARGET_BOOTNUM ]]; then
            if [[ $num == "$TARGET_BOOTNUM" ]]; then
                hits+=("$num"$'\t'"$flag"$'\t'"$label")
            fi
        else
            shopt -s nocasematch
            # TARGET_MATCH is a glob pattern on purpose, so it must stay unquoted here.
            # shellcheck disable=SC2053
            if [[ $label == $TARGET_MATCH ]]; then
                hits+=("$num"$'\t'"$flag"$'\t'"$label")
            fi
            shopt -u nocasematch
        fi
    done <<< "$entries"
    if [[ ${#hits[@]} -gt 0 ]]; then
        printf '%s\n' "${hits[@]}"
    fi
}

do_reboot() {
    info "rebooting now."
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl reboot 2>/dev/null; then return 0; fi
        if "${SUDO[@]}" systemctl reboot; then return 0; fi
    fi
    if "${SUDO[@]}" reboot; then return 0; fi
    die "could not reboot. BootNext is still armed, so the next manual reboot boots Windows once (disarm with: ${SUDO_DISPLAY}${EFIBOOTMGR} --delete-bootnext)."
}

on_abort() {
    trap - INT TERM
    printf '\n' >&2
    warn "aborted - clearing BootNext again so the next reboot is a normal one..."
    if "${SUDO[@]}" "$EFIBOOTMGR" --delete-bootnext >/dev/null; then
        info "BootNext cleared."
    else
        warn "could not clear BootNext; clear it manually: ${SUDO_DISPLAY}${EFIBOOTMGR} --delete-bootnext"
    fi
    exit 130
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
if [[ -n $OPT_CONFIG ]]; then
    CONFIG_FILE="$(readlink -f -- "$OPT_CONFIG")"
else
    CONFIG_FILE="$SCRIPT_DIR/$DEFAULT_CONFIG_NAME"
fi

RAW="$(read_efibootmgr)" || die "efibootmgr could not read the EFI boot variables."
ENTRIES="$(parse_entries "$RAW")"
[[ -n $ENTRIES ]] || die "no Boot#### entries found in the efibootmgr output:
$RAW"

if (( OPT_LIST )); then
    printf '\nFirmware boot entries (efibootmgr):\n\n'
    print_entries "$ENTRIES"
    printf '\n'
    grep -E '^(BootCurrent|BootNext|BootOrder|Timeout):' <<< "$RAW" || true
    printf '\nPut the LABEL of the Windows entry into TARGET_MATCH (or its number into TARGET_BOOTNUM).\n'
    printf 'Config file that would be used: %s\n' "$CONFIG_FILE"
    printf 'Full details (device paths): %s%s -v\n' "$SUDO_DISPLAY" "$EFIBOOTMGR"
    exit 0
fi

load_config "$CONFIG_FILE"
validate_settings

if [[ -n $TARGET_BOOTNUM ]]; then
    CRITERIA="TARGET_BOOTNUM=$TARGET_BOOTNUM"
else
    CRITERIA="TARGET_MATCH=\"$TARGET_MATCH\""
fi

mapfile -t HITS < <(select_targets "$ENTRIES")
if [[ ${#HITS[@]} -eq 0 ]]; then
    print_entries "$ENTRIES" >&2
    die "no firmware boot entry matches $CRITERIA. Pick one of the entries above and update $CONFIG_FILE."
elif [[ ${#HITS[@]} -gt 1 ]]; then
    print_entries "$(printf '%s\n' "${HITS[@]}")" >&2
    die "${#HITS[@]} entries match $CRITERIA (listed above). Make the pattern more specific or set TARGET_BOOTNUM in $CONFIG_FILE."
fi

IFS=$'\t' read -r TARGET_NUM TARGET_FLAG TARGET_LABEL <<< "${HITS[0]}"
info "target entry: Boot$TARGET_NUM ($TARGET_FLAG)  $TARGET_LABEL"
if [[ $TARGET_FLAG != active ]]; then
    warn "entry Boot$TARGET_NUM is inactive; some firmware ignores BootNext for inactive entries. Activate it with: ${SUDO_DISPLAY}${EFIBOOTMGR} --bootnum $TARGET_NUM --active"
fi

if (( OPT_DRY_RUN )); then
    info "dry run - nothing will be changed. The real run would execute:"
    printf '    %s%s --bootnext %s\n' "$SUDO_DISPLAY" "$EFIBOOTMGR" "$TARGET_NUM" >&2
    if (( OPT_NO_REBOOT )); then
        printf '    (no reboot because of --no-reboot)\n' >&2
    else
        printf '    sleep %s && systemctl reboot\n' "$REBOOT_DELAY" >&2
    fi
    exit 0
fi

info "setting BootNext to Boot$TARGET_NUM ($TARGET_LABEL)..."
"${SUDO[@]}" "$EFIBOOTMGR" --bootnext "$TARGET_NUM" >/dev/null || die "'efibootmgr --bootnext $TARGET_NUM' failed."

AFTER="$(read_efibootmgr || true)"
if grep -qiE "^BootNext: *${TARGET_NUM}\$" <<< "$AFTER"; then
    info "BootNext is armed: the next boot starts '$TARGET_LABEL' once."
else
    warn "efibootmgr reported success, but 'BootNext: $TARGET_NUM' is not visible in its output. Current state:"
    grep -E '^(BootCurrent|BootNext|BootOrder):' <<< "$AFTER" >&2 || true
fi

if (( OPT_NO_REBOOT )); then
    info "not rebooting (--no-reboot). The next reboot or power-on boots Windows once."
    info "to disarm it instead: ${SUDO_DISPLAY}${EFIBOOTMGR} --delete-bootnext"
    exit 0
fi

if (( REBOOT_DELAY > 0 )); then
    trap on_abort INT TERM
    info "rebooting into '$TARGET_LABEL' in $REBOOT_DELAY second(s)... press Ctrl+C to abort (BootNext is then cleared again)."
    sleep "$REBOOT_DELAY"
    trap - INT TERM
fi
do_reboot
