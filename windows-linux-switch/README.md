# windows-linux-switch

Two small scripts for a dual-boot PC where **Windows and NixOS sit on separate drives**, each with its own
bootloader on its own EFI System Partition (ESP):

| Script                                     | Runs on              | What it does                       |
| ------------------------------------------ | -------------------- | ---------------------------------- |
| [`boot-to-nixos.ps1`](boot-to-nixos.ps1)   | Windows (PowerShell) | restart straight into NixOS, once  |
| [`boot-to-windows.sh`](boot-to-windows.sh) | NixOS (bash)         | reboot straight into Windows, once |

Both do the same thing from opposite sides: they set the UEFI firmware's one-shot **`BootNext`** variable to the
other OS's boot entry and reboot. Nothing permanent changes. After that single boot the firmware is back to its
normal default order, no bootloader menu is involved, and neither OS has to know about the other.

> **This repository is public.** The committed scripts contain nothing machine-specific. Every value that
> identifies _your_ machine (entry names, GUIDs, entry numbers) lives in a git-ignored config file next to each
> script. The committed `*.example.*` files hold `REPLACE_ME` placeholders only.
> Section 3 explains how to fill them in, section 7 what to change when your setup changes.

## Contents

- [windows-linux-switch](#windows-linux-switch)
  - [Contents](#contents)
  - [1. How it works](#1-how-it-works)
  - [2. Files](#2-files)
  - [3. Setup](#3-setup)
    - [3.1 Requirements](#31-requirements)
    - [3.2 Windows side](#32-windows-side)
    - [3.3 NixOS side](#33-nixos-side)
  - [4. Configuration reference](#4-configuration-reference)
    - [4.1 `boot-to-nixos.psd1` (Windows side)](#41-boot-to-nixospsd1-windows-side)
    - [4.2 `boot-to-windows.conf` (NixOS side)](#42-boot-to-windowsconf-nixos-side)
  - [5. Command-line reference](#5-command-line-reference)
    - [`boot-to-nixos.ps1` (Windows)](#boot-to-nixosps1-windows)
    - [`boot-to-windows.sh` (NixOS)](#boot-to-windowssh-nixos)
  - [6. Doing it by hand, checking, undoing](#6-doing-it-by-hand-checking-undoing)
  - [7. When your setup changes](#7-when-your-setup-changes)
    - [7.1 Creating a missing firmware entry](#71-creating-a-missing-firmware-entry)
  - [8. Troubleshooting](#8-troubleshooting)
  - [9. Security and privacy notes](#9-security-and-privacy-notes)
  - [10. Alternatives that were not chosen](#10-alternatives-that-were-not-chosen)
  - [11. References](#11-references)

## 1. How it works

A UEFI firmware keeps its own list of boot entries in NVRAM, independent of any operating system:

```
Boot0000* Windows Boot Manager   HD(1,GPT,...)/File(\EFI\Microsoft\Boot\bootmgfw.efi)   <- Windows drive, its ESP
Boot0001* Linux Boot Manager     HD(1,GPT,...)/File(\EFI\systemd\systemd-bootx64.efi)   <- NixOS drive, its ESP
BootOrder: 0000,0001             <- permanent default order (what you edit in the firmware setup)
BootNext:  0001                  <- "boot THIS entry next time, once, then forget it"
```

- `BootOrder` is the permanent order the firmware tries.
- `BootNext` is a one-shot override: the firmware boots that entry once and deletes the variable.

Each script writes `BootNext` and reboots. That is all:

| Direction        | Command that does the work                                                                           | Reboot             |
| ---------------- | ---------------------------------------------------------------------------------------------------- | ------------------ |
| Windows -> NixOS | `bcdedit /set {fwbootmgr} bootsequence {GUID}` (Windows writes this as the UEFI `BootNext` variable) | `shutdown /r`      |
| NixOS -> Windows | `efibootmgr --bootnext XXXX`                                                                         | `systemctl reboot` |

Why this is the right approach for separate drives:

- **Bootloader-agnostic.** systemd-boot, GRUB or anything else: the bootloader is never involved. Windows does not
  need a NixOS entry in its boot menu and NixOS does not need to chainload Windows.
- **Nothing permanent.** `BootOrder` is untouched. If the chosen entry fails to boot, the firmware falls back to
  the normal order.
- **Immune to Windows "fixing" the boot order** after feature updates, because the scripts do not depend on the
  order at all.

On top of that the scripts add: entry lookup by label pattern (no numbers or GUIDs to hardcode), a list mode to
discover the right values, a dry-run mode, verification after writing the variable, and self-elevation on Windows.

## 2. Files

| File                           | Committed?           | Purpose                                                                     |
| ------------------------------ | -------------------- | --------------------------------------------------------------------------- |
| `boot-to-nixos.ps1`            | yes                  | Windows -> NixOS script                                                     |
| `boot-to-nixos.example.psd1`   | yes                  | template for the Windows config (placeholders only)                         |
| `boot-to-nixos.psd1`           | **no** (git-ignored) | your real Windows config                                                    |
| `boot-to-windows.sh`           | yes                  | NixOS -> Windows script                                                     |
| `boot-to-windows.example.conf` | yes                  | template for the NixOS config (placeholders only)                           |
| `boot-to-windows.conf`         | **no** (git-ignored) | your real NixOS config                                                      |
| `.gitignore`                   | yes                  | ignores the two real config files                                           |
| `.gitattributes`               | yes                  | forces LF line endings for the bash side (CRLF breaks the shebang on NixOS) |
| `README.md`                    | yes                  | this manual                                                                 |

## 3. Setup

### 3.1 Requirements

Both sides:

- Both operating systems are installed and **booted in UEFI mode** (not legacy BIOS / CSM).
  Windows: `msinfo32` shows "BIOS Mode: UEFI", or `$env:firmware_type` in PowerShell prints `UEFI`.
  NixOS: the directory `/sys/firmware/efi` exists.
- Each OS has **its own entry in the firmware's boot list**. The list modes (`-List` / `--list`) show it.
  If an entry is missing, see [7.1](#71-creating-a-missing-firmware-entry).
- Secure Boot: if NixOS boots today, you are fine. If Secure Boot is on and the NixOS loader is unsigned, the
  NixOS entry fails and the firmware silently falls back to Windows (Lanzaboote is the usual NixOS answer).

Windows side:

- Windows PowerShell 5.1 (built into Windows 10/11) or PowerShell 7. Nothing to install.
- Administrator rights: `bcdedit` needs them. The script requests elevation itself (UAC prompt).

NixOS side:

- `efibootmgr`. Recommended: `environment.systemPackages = [ pkgs.efibootmgr ];`. Without it the script fetches
  the package from nixpkgs on the fly (`nix build nixpkgs#efibootmgr`), which works but is slower.
- `sudo` (NixOS default) or `doas`. It is only used to run `efibootmgr`; `systemctl reboot` is tried without it first.
- The script is plain bash + coreutils and works on any systemd-based Linux, not only NixOS.

### 3.2 Windows side

1. Get this folder onto the Windows drive (clone the repo or copy the folder).
2. Create your config from the template. The real file name is git-ignored:

   ```powershell
   Copy-Item .\boot-to-nixos.example.psd1 .\boot-to-nixos.psd1
   ```

3. List the firmware entries (a UAC prompt appears, the output opens in an elevated window):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\boot-to-nixos.ps1 -List
   ```

   Example output:

   ```
   {12345678-1234-1234-1234-123456789abc}
       device         partition=\Device\HarddiskVolume5
       path           \EFI\systemd\systemd-bootx64.efi
       description    Linux Boot Manager

   {bootmgr}
       device         partition=\Device\HarddiskVolume1
       path           \EFI\Microsoft\Boot\bootmgfw.efi
       description    Windows Boot Manager
       ...
   ```

   Find the entry that boots NixOS. With systemd-boot and `boot.loader.efi.canTouchEfiVariables = true` it is
   `Linux Boot Manager`; with GRUB it is `NixOS-boot`; with `canTouchEfiVariables = false` it is an entry the
   firmware generated itself, often called `UEFI OS` or `UEFI: <disk model>`. A `path` ending in
   `systemd-bootx64.efi`, `grubx64.efi` or `BOOTX64.EFI` on a non-Windows volume is the other giveaway.
   The full table is in [4.1](#41-boot-to-nixospsd1-windows-side).

4. Open `boot-to-nixos.psd1` and set `TargetMatch` to that description (wildcards allowed):

   ```powershell
   TargetMatch = 'Linux Boot Manager'
   ```

5. Dry run (resolves the entry, changes nothing). It must report exactly one target entry:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\boot-to-nixos.ps1 -DryRun
   ```

6. The real thing. Save your work first: a timed restart closes programs without asking.

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\boot-to-nixos.ps1
   ```

Convenience:

- Execution policy: if `.\boot-to-nixos.ps1` is refused ("running scripts is disabled on this system"), either call
  it through `powershell -ExecutionPolicy Bypass -File ...` as above, or run once
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- Desktop / Start-menu shortcut: create a shortcut whose target is
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\windows-linux-switch\boot-to-nixos.ps1"`.
  The script asks for elevation by itself; optionally tick "Run as administrator" under
  Properties -> Advanced to skip the extra console window.
- `-Delay 0` restarts immediately. `-NoReboot` only arms BootNext, so your next normal restart lands in NixOS.

### 3.3 NixOS side

1. Get this folder onto NixOS (clone the repo).
2. Recommended NixOS configuration:

   ```nix
   # configuration.nix
   environment.systemPackages = [ pkgs.efibootmgr ];
   ```

   Optional, if you do not want a sudo password prompt every time. Only `efibootmgr` is allowed, nothing else:

   ```nix
   security.sudo.extraRules = [{
     users = [ "YOUR_USERNAME" ];
     commands = [{
       command = "/run/current-system/sw/bin/efibootmgr";
       options = [ "NOPASSWD" ];
     }];
   }];
   ```

3. Create your config from the template (the real file name is git-ignored) and make the script executable:

   ```bash
   cp boot-to-windows.example.conf boot-to-windows.conf
   chmod +x boot-to-windows.sh
   ```

4. List the firmware entries:

   ```bash
   ./boot-to-windows.sh --list
   ```

   Example output:

   ```
   ENTRY     STATE     LABEL
   Boot0000  active    Windows Boot Manager
   Boot0001  active    Linux Boot Manager

   BootCurrent: 0001
   BootOrder: 0001,0000
   ```

5. Open `boot-to-windows.conf` and set `TARGET_MATCH` to the label of the Windows entry:

   ```
   TARGET_MATCH="Windows Boot Manager"
   ```

6. Dry run, then the real thing:

   ```bash
   ./boot-to-windows.sh --dry-run
   ./boot-to-windows.sh
   ```

Convenience:

- Alias: `alias boot-to-windows='/path/to/windows-linux-switch/boot-to-windows.sh'`, or symlink the script into
  `~/.local/bin`.
- Keep the config outside the repo if you prefer: `./boot-to-windows.sh --config ~/.config/boot-to-windows.conf`.
- Declarative wrapper, if you want a `boot-to-windows` command from `configuration.nix`
  (`/path/to/...` must exist at run time; adjust it):

  ```nix
  environment.etc."boot-to-windows.conf".text = ''
    TARGET_MATCH="Windows Boot Manager"
    REBOOT_DELAY=5
  '';
  environment.systemPackages = [
    pkgs.efibootmgr
    (pkgs.writeShellScriptBin "boot-to-windows" ''
      exec ${pkgs.bash}/bin/bash /path/to/windows-linux-switch/boot-to-windows.sh \
        --config /etc/boot-to-windows.conf "$@"
    '')
  ];
  ```

## 4. Configuration reference

### 4.1 `boot-to-nixos.psd1` (Windows side)

A PowerShell data file: one hashtable, read with `Import-PowerShellDataFile` (data only, no code is executed).
Strings go in single quotes.

| Key           | Type                     | Meaning                                                                                                                                                                                                                                                                  | How to find the value                                                                                                   |
| ------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `TargetMatch` | string, wildcard pattern | Must match one field (normally the `description`) of exactly one firmware entry. `*` and `?` are wildcards, matching is case-insensitive, and the whole field must match: `'Linux*'` matches `Linux Boot Manager`, `'Linux'` does not. Ignored when `TargetGuid` is set. | `-List`, then copy the `description` of the NixOS entry. Matching the loader path also works: `'*systemd-bootx64.efi'`. |
| `TargetGuid`  | string `'{...}'`         | Exact identifier of the entry. Overrides `TargetMatch`. Only for cases where no pattern is unique. Windows generates this GUID when it first sees the firmware entry, and it changes whenever the entry is re-created.                                                   | `-List`, the `{...}` line above the entry's fields.                                                                     |
| `RebootDelay` | integer >= 0             | Seconds between arming BootNext and the restart. `0` restarts immediately (then `shutdown /a` cannot abort it any more).                                                                                                                                                 | your choice; `-Delay N` overrides it per run                                                                            |

Typical `TargetMatch` values, depending on how NixOS is booted:

| NixOS bootloader setup                                                                                                     | Firmware entry description                                                                                                                         | Loader path                               |
| -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| systemd-boot with `boot.loader.efi.canTouchEfiVariables = true` (`bootctl install` registers the entry)                    | `Linux Boot Manager`                                                                                                                               | `\EFI\systemd\systemd-bootx64.efi`        |
| GRUB with `boot.loader.grub.efiSupport = true` and `canTouchEfiVariables = true`                                           | `NixOS-boot`. This is `system.nixos.distroName` plus the ESP mount point with `/` replaced by `-`; `boot.loader.grub.efiBootloaderId` overrides it | `\EFI\NixOS-boot\grubx64.efi`             |
| `canTouchEfiVariables = false` (NixOS creates no entry; the firmware boots the fallback loader and names the entry itself) | firmware-specific, e.g. `UEFI OS` or `UEFI: Samsung SSD 980 PRO 1TB`                                                                               | `\EFI\BOOT\BOOTX64.EFI`, or no path shown |
| another distribution                                                                                                       | its own label, e.g. `ubuntu`, `Fedora`, `GRUB`                                                                                                     | `\EFI\<distro>\...`                       |

### 4.2 `boot-to-windows.conf` (NixOS side)

Plain `KEY=value` lines. The file is parsed, not sourced: no shell expansion happens, `#` lines are comments,
one pair of surrounding quotes is stripped.

| Key              | Meaning                                                                                                                                                                          | How to find the value                                                                |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `TARGET_MATCH`   | Glob pattern that must match the **whole label** of exactly one `Boot####` entry. `*` and `?` are wildcards, matching is case-insensitive. Ignored when `TARGET_BOOTNUM` is set. | `--list`, then copy the LABEL of the Windows entry. Normally `Windows Boot Manager`. |
| `TARGET_BOOTNUM` | Exact entry number, `0000` or `Boot0000`. Overrides `TARGET_MATCH`. Only when no pattern is unique. Numbers can change when entries are re-created.                              | `--list`, the ENTRY column                                                           |
| `REBOOT_DELAY`   | Seconds between arming BootNext and the reboot. Ctrl+C during the countdown disarms BootNext again.                                                                              | your choice; `--delay N` overrides it per run                                        |

## 5. Command-line reference

### `boot-to-nixos.ps1` (Windows)

| Option         | Effect                                                                         |
| -------------- | ------------------------------------------------------------------------------ |
| (none)         | arm BootNext for the configured entry, restart after `RebootDelay` seconds     |
| `-List`        | show all firmware entries plus the firmware boot manager state; change nothing |
| `-DryRun`      | resolve the target entry and print the commands that would run; change nothing |
| `-NoReboot`    | arm BootNext only; your next restart or power-on boots NixOS                   |
| `-Delay N`     | override `RebootDelay`                                                         |
| `-Config PATH` | use another config file                                                        |
| `-PauseOnExit` | internal, added when the script relaunches itself elevated                     |

Exit code 0 on success, 1 on any error. When started without administrator rights, the script relaunches itself
through UAC; the original window exits at once and all output appears in the elevated window, which waits for
Enter before closing.

### `boot-to-windows.sh` (NixOS)

| Option                         | Effect                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------ |
| (none)                         | arm BootNext for the configured entry, reboot after `REBOOT_DELAY` seconds     |
| `-l`, `--list`                 | show all firmware entries; change nothing                                      |
| `-n`, `--dry-run`              | resolve the target entry and print the commands that would run; change nothing |
| `-N`, `--no-reboot`            | arm BootNext only                                                              |
| `-d N`, `--delay N`            | override `REBOOT_DELAY`                                                        |
| `-c FILE`, `--config FILE`     | use another config file                                                        |
| `-h`, `--help`                 | help                                                                           |
| environment `EFIBOOTMGR=/path` | use a specific `efibootmgr` binary                                             |

Exit code 0 on success, 1 on any error, 130 when aborted with Ctrl+C during the countdown.

## 6. Doing it by hand, checking, undoing

Windows, in an elevated PowerShell. The braces must be quoted in PowerShell (not in `cmd`), otherwise PowerShell
treats `{fwbootmgr}` as a script block and bcdedit receives `fwbootmgr` without braces:

| Task                                                                             | Command                                                      |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| list firmware entries                                                            | `bcdedit /enum firmware`                                     |
| show the firmware boot manager (permanent order plus a pending BootNext, if any) | `bcdedit /enum '{fwbootmgr}'`                                |
| arm BootNext                                                                     | `bcdedit /set '{fwbootmgr}' bootsequence '{GUID}'`           |
| disarm BootNext                                                                  | `bcdedit /deletevalue '{fwbootmgr}' bootsequence`            |
| abort a pending timed restart                                                    | `shutdown /a`                                                |
| restart into the firmware setup instead                                          | `shutdown /r /fw /t 0`                                       |
| change the permanent default (the scripts never do this)                         | `bcdedit /set '{fwbootmgr}' displayorder '{GUID}' /addfirst` |

NixOS:

| Task                                                     | Command                                           |
| -------------------------------------------------------- | ------------------------------------------------- |
| list entries with their device paths                     | `sudo efibootmgr -v`                              |
| arm BootNext                                             | `sudo efibootmgr --bootnext 0000`                 |
| disarm BootNext                                          | `sudo efibootmgr --delete-bootnext`               |
| activate an inactive entry                               | `sudo efibootmgr --bootnum 0000 --active`         |
| change the permanent default (the scripts never do this) | `sudo efibootmgr --bootorder 0001,0000`           |
| delete a stale entry                                     | `sudo efibootmgr --bootnum 0003 --delete-bootnum` |
| reboot into the firmware setup                           | `systemctl reboot --firmware-setup`               |

## 7. When your setup changes

Rule of thumb: **re-run the list mode on the side that targets the thing you changed, then update that side's
config.** The Windows config describes the NixOS entry; the NixOS config describes the Windows entry.

| What changed                                                                        | Windows config `boot-to-nixos.psd1`                                                                                                                                 | NixOS config `boot-to-windows.conf`                                                                                                                     |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| NixOS moved to another drive, or reinstalled                                        | re-run `-List`. The description is usually unchanged (`Linux Boot Manager`), so a `TargetMatch` keeps working. A `TargetGuid` is now stale: replace it or clear it. | nothing, unless you used `TARGET_BOOTNUM` (numbers may shift): re-run `--list`                                                                          |
| NixOS bootloader changed (systemd-boot <-> GRUB), or `canTouchEfiVariables` toggled | the description changes (table in 4.1): re-run `-List`, update `TargetMatch`                                                                                        | nothing                                                                                                                                                 |
| ESP mount point changed with GRUB (`boot.loader.efi.efiSysMountPoint`)              | the GRUB id suffix changes (`NixOS-boot` -> `NixOS-efi` and so on): update `TargetMatch`                                                                            | nothing                                                                                                                                                 |
| Windows reinstalled, or moved to another drive                                      | GUIDs regenerate: only matters if you used `TargetGuid`                                                                                                             | re-run `--list`. The label is normally still `Windows Boot Manager`; a `TARGET_BOOTNUM` may have changed                                                |
| NixOS replaced by another Linux                                                     | `TargetMatch` becomes that distribution's label (`ubuntu`, `Fedora`, `GRUB`, ...)                                                                                   | the script works on any systemd Linux and the config stays the same; install `efibootmgr` with that distribution's package manager, or set `EFIBOOTMGR` |
| firmware update or CMOS reset wiped the entries                                     | re-create the entries (7.1), then `-List` again: the GUIDs are new                                                                                                  | `--list` again: the numbers are new                                                                                                                     |
| two entries with the same label (for example two `Windows Boot Manager`)            | use `TargetGuid`, or delete the stale one from NixOS                                                                                                                | use `TARGET_BOOTNUM`, or delete the stale one: `sudo efibootmgr -b XXXX -B`                                                                             |
| a third OS                                                                          | one config file per target: `-Config other.psd1` (add the name to `.gitignore`)                                                                                     | `--config other.conf` (add the name to `.gitignore`)                                                                                                    |
| different drive letters, volume numbers, disk models                                | nothing: the scripts never use them                                                                                                                                 | nothing                                                                                                                                                 |

### 7.1 Creating a missing firmware entry

If `-List` / `--list` does not show an entry for an OS, the firmware has none for it. This is common with
`canTouchEfiVariables = false`, after a firmware reset, or when an ESP was re-created.

Preferred: from NixOS (or from a NixOS live USB).

- Let NixOS do it: set `boot.loader.efi.canTouchEfiVariables = true;` and run
  `sudo nixos-rebuild switch --install-bootloader`. systemd-boot registers `Linux Boot Manager`, GRUB registers
  `NixOS-boot`.
- By hand, for example systemd-boot on partition 1 of `/dev/nvme1n1`
  (`lsblk -o NAME,PARTTYPENAME,FSTYPE,SIZE` shows which partition is which ESP):

  ```bash
  sudo efibootmgr --create --disk /dev/nvme1n1 --part 1 --label "Linux Boot Manager" --loader '\EFI\systemd\systemd-bootx64.efi'
  ```

- A Windows entry, if Windows' own entry is gone (ESP = partition 1 of `/dev/nvme0n1` here):

  ```bash
  sudo efibootmgr --create --disk /dev/nvme0n1 --part 1 --label "Windows Boot Manager" --loader '\EFI\Microsoft\Boot\bootmgfw.efi'
  ```

Last resort: from Windows (elevated `cmd`, so no quoting of braces). Windows pushes new boot manager objects from
its BCD store into the firmware's NVRAM. Give the NixOS ESP a drive letter first (`mountvol` lists the volumes,
`mountvol S: \\?\Volume{...}\` mounts one):

```
bcdedit /copy {bootmgr} /d "Linux Boot Manager"        (prints the new {GUID})
bcdedit /set {GUID} device partition=S:
bcdedit /set {GUID} path \EFI\systemd\systemd-bootx64.efi
bcdedit /set {fwbootmgr} displayorder {GUID} /addlast
```

Verify with `bcdedit /enum firmware` and, better, with `sudo efibootmgr -v` from NixOS. If the copy carries over
values from the Windows entry that do not belong there (`default`, `displayorder`, `toolsdisplayorder`,
`timeout`), remove them with `bcdedit /deletevalue {GUID} <name>`.

## 8. Troubleshooting

| Symptom                                                                                          | Likely cause                                                                                                                                                                                                                    | Fix                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the machine just boots the same OS again                                                         | the target entry does not actually boot (Secure Boot rejects an unsigned loader, loader file moved, ESP re-created), so the firmware falls back to `BootOrder`; or the entry is inactive; rarely, a firmware ignores `BootNext` | boot the target from the firmware's boot menu (F8 / F11 / F12 / Esc at power-on) to see the real error; `sudo efibootmgr -b XXXX -a` for inactive entries; firmware update                                                    |
| Windows: `The boot configuration data store could not be opened. Access is denied.`              | not elevated                                                                                                                                                                                                                    | run the script normally, it elevates itself; for manual commands use an elevated terminal                                                                                                                                     |
| Windows: bcdedit complains about an invalid identifier and the braces are missing in the message | PowerShell parsed `{fwbootmgr}` as a script block                                                                                                                                                                               | quote it: `'{fwbootmgr}'` (the script already does)                                                                                                                                                                           |
| Windows: `running scripts is disabled on this system`                                            | execution policy                                                                                                                                                                                                                | `powershell -ExecutionPolicy Bypass -File .\boot-to-nixos.ps1 ...`                                                                                                                                                            |
| Windows: the NixOS entry is missing from `-List`, but `efibootmgr` on NixOS shows it             | Windows mirrors the firmware entries into its BCD store when bcdedit runs; an inactive entry or one outside `BootOrder` may not be mirrored                                                                                     | from NixOS: `sudo efibootmgr -b XXXX -a`, then `sudo efibootmgr -o <current order>,XXXX`; run `-List` again                                                                                                                   |
| `0 entries match` / `2 entries match`                                                            | pattern too strict or too loose, or duplicate entries                                                                                                                                                                           | adjust the pattern, or pin the entry with `TargetGuid` / `TARGET_BOOTNUM`; delete duplicates from NixOS                                                                                                                       |
| NixOS: `efibootmgr was not found`                                                                | package not installed and the `nix` fallback failed                                                                                                                                                                             | `environment.systemPackages = [ pkgs.efibootmgr ];`, or `EFIBOOTMGR=/path ./boot-to-windows.sh`                                                                                                                               |
| NixOS: `EFI variables are not supported on this system`                                          | not booted in UEFI mode, or efivarfs not mounted                                                                                                                                                                                | check `/sys/firmware/efi`; boot the NixOS drive in UEFI mode, not via CSM                                                                                                                                                     |
| NixOS: `Could not set BootNext: Read-only file system` or `Operation not permitted`              | efivarfs mounted read-only, or kernel lockdown                                                                                                                                                                                  | `sudo mount -o remount,rw /sys/firmware/efi/efivars`; check `dmesg`                                                                                                                                                           |
| NixOS: `systemctl reboot` asks for authentication or fails                                       | no active local session, polkit denies it                                                                                                                                                                                       | the script retries with sudo; `sudo systemctl reboot` works manually                                                                                                                                                          |
| NixOS sees the Windows partitions as read-only or "hibernated" after a Windows shutdown          | Windows Fast Startup (hybrid shutdown)                                                                                                                                                                                          | the scripts always _restart_, which is unaffected, but for dual boot disable Fast Startup anyway: Control Panel -> Power Options -> "Choose what the power buttons do" -> untick "Turn on fast startup", or `powercfg /h off` |
| the clock is off by hours after switching                                                        | Windows keeps the hardware clock in local time, Linux in UTC                                                                                                                                                                    | NixOS: `time.hardwareClockInLocalTime = true;`, or make Windows use UTC (`RealTimeIsUniversal` DWORD = 1 under `HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation`)                                                   |
| after a Windows feature update the PC boots Windows by default again                             | Windows put itself first in `BootOrder`                                                                                                                                                                                         | irrelevant for these scripts; restore with `sudo efibootmgr -o 0001,0000` if you want NixOS first                                                                                                                             |

## 9. Security and privacy notes

- `boot-to-nixos.psd1` and `boot-to-windows.conf` are git-ignored. Keep it that way: GUIDs, volume numbers and
  disk model names identify your hardware. Do not paste `-List` / `--list` output into public issues either.
- The scripts write exactly one firmware variable, `BootNext`, which the firmware deletes after use. They never
  touch `BootOrder`, the Windows BCD entries, partitions or files.
- The config files are data, not code: the `.psd1` is read with `Import-PowerShellDataFile` (restricted language,
  nothing is executed) and the `.conf` is parsed line by line, not sourced. A tampered config file cannot run
  anything with the elevated rights the scripts hold.
- Windows: elevation goes through a visible UAC prompt. NixOS: sudo/doas is used only for `efibootmgr`, and as a
  fallback for `systemctl reboot`.

## 10. Alternatives that were not chosen

- **systemd-boot's auto-detected Windows entry plus `systemctl reboot --boot-loader-entry=auto-windows`.**
  systemd-boot only detects a Windows loader that sits on _its own_ ESP. With Windows on another drive there is no
  such entry (copying `\EFI\Microsoft` over is a hack that breaks on Windows updates).
- **GRUB with os-prober and `grub-reboot`.** Works across drives, but ties everything to GRUB and to os-prober
  finding Windows. Firmware-level BootNext needs none of that.
- **A NixOS entry in the Windows boot menu plus `bcdedit /bootsequence`.** Requires chainloading the NixOS loader
  from the Windows boot manager. Fragile and Windows-specific.
- **A third-party boot manager such as rEFInd.** A nice menu, but it still cannot be _told_ from inside an OS where
  to go next without BootNext.

## 11. References

- bcdedit command-line options (`/set`, `/enum`, `/deletevalue`):
  <https://learn.microsoft.com/windows-hardware/drivers/devtest/bcdedit-command-line-options>
- `bcdedit /enum firmware` output format and how Windows syncs BCD with NVRAM:
  <https://learn.microsoft.com/previous-versions/windows/it-pro/windows-vista/cc749510(v=ws.10)>
- efibootmgr(8): <https://man.archlinux.org/man/efibootmgr.8>
- bootctl(1), `--efi-boot-option-description` defaults to `Linux Boot Manager`:
  <https://www.freedesktop.org/software/systemd/man/latest/bootctl.html>
- NixOS options `boot.loader.efi.canTouchEfiVariables`, `boot.loader.grub.efiBootloaderId`,
  `boot.loader.systemd-boot.*`: <https://search.nixos.org/options>
- Other projects using the same `{fwbootmgr} bootsequence` approach:
  <https://github.com/jungin500/efinextboot>, <https://github.com/grexai/SteamDeck-changeOS>
