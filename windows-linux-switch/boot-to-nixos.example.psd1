@{
    # -------------------------------------------------------------------------
    # boot-to-nixos.ps1 configuration  (PowerShell data file: data only, no code runs)
    #
    #   1. Copy this file to  boot-to-nixos.psd1  in the same folder as the script.
    #      That file name is git-ignored, so your machine-specific values stay local.
    #   2. Run  .\boot-to-nixos.ps1 -List  and find the entry that boots NixOS.
    #   3. Replace every REPLACE_ME below. Full manual: README.md, section "Configuration".
    # -------------------------------------------------------------------------

    # Wildcard pattern (case-insensitive; * and ? allowed) that must match ONE field of
    # exactly one firmware boot entry - normally its "description" as shown by -List.
    # Typical values:
    #   systemd-boot, boot.loader.efi.canTouchEfiVariables = true ... 'Linux Boot Manager'
    #   GRUB (NixOS default id, ESP mounted at /boot) ................ 'NixOS-boot'
    #   firmware-generated fallback entry (canTouchEfiVariables = false) 'UEFI OS' or 'UEFI: <disk model>*'
    #   match on the loader path instead of the description .......... '*systemd-bootx64.efi'
    TargetMatch = 'REPLACE_ME'

    # Optional: the exact identifier GUID of the entry, as shown by -List,
    # e.g. '{12345678-1234-1234-1234-123456789abc}'. When set, TargetMatch is ignored.
    # Use it only when TargetMatch cannot be made unique. Leave '' otherwise.
    TargetGuid = ''

    # Seconds to wait before restarting (0 = immediately). Override per run with -Delay.
    RebootDelay = 5
}
