# run with: nix-shell <filename>.nix
let
  pkgs = import <nixpkgs> { };
in
pkgs.mkShell {
  packages = [
    pkgs.zsh

    # other packages you need
    # python
    # (pkgs.python3.withPackages (pp: [
    #   pp.pandas
    #   pp.requests
    #   pp.numpy
    # ]))

    # node
    # pkgs.nodejs_24

    # go
    # pkgs.go
  ];

  shellHook = ''
    export SHELL=${pkgs.zsh}/bin/zsh
    export ZDOTDIR=$(pwd)/.zshrc.d
    mkdir -p $ZDOTDIR
    cat > $ZDOTDIR/.zshrc <<'EOF'
      # Source your original config
      [[ -f ~/.zshrc ]] && source ~/.zshrc

      # Prepend plain white (nix-shell) to the existing prompt
      PROMPT="%F{white}(nix-shell)%f $PROMPT"
    EOF
    exec ${pkgs.zsh}/bin/zsh
  '';
}
