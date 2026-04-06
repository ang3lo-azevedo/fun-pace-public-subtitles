{
  description = "Fun Pace subtitle pipeline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          termsFile = pkgs.writeText "one-piece-terms.tsv" (builtins.readFile ./data/one-piece-terms.tsv);
          ldLibraryPath = pkgs.lib.makeLibraryPath [
            pkgs.stdenv.cc.cc.lib
            pkgs.ffmpeg_7
            pkgs.zlib
            pkgs.zstd
            pkgs.rocmPackages.clr
            pkgs.rocmPackages.hipblas
            pkgs.rocmPackages.hiprand
            pkgs.rocmPackages.rocblas
            pkgs.rocmPackages.hipsparse
            pkgs.rocmPackages.hipsolver
            pkgs.rocmPackages.miopen
            pkgs.rocmPackages.rocm-runtime
          ];
          runtimeInputs = [
            pkgs.coreutils
            pkgs.ffmpeg_7
            pkgs.gawk
            pkgs.gnused
            pkgs.mkvtoolnix
            pkgs.python3
            pkgs.stdenv.cc.cc.lib
            pkgs.uv
          ];
          scriptText = pkgs.lib.replaceStrings
            [ "@DEFAULT_TERMS_FILE@" "@DEFAULT_LD_LIBRARY_PATH@" ]
            [ "${termsFile}" "${ldLibraryPath}" ]
            (builtins.readFile ./scripts/fun-pace-subs);
        in
        {
          default = pkgs.symlinkJoin {
            name = "fun-pace-subs";
            paths = [ (pkgs.writeScriptBin "fun-pace-subs" scriptText) ];
            buildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              wrapProgram "$out/bin/fun-pace-subs" \
                --prefix PATH : ${pkgs.lib.makeBinPath runtimeInputs} \
                --set FUN_PACE_DEFAULT_TERMS_FILE "${termsFile}" \
                --set FUN_PACE_DEFAULT_LD_LIBRARY_PATH "${ldLibraryPath}"
            '';
          };
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/fun-pace-subs";
        };
      });

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          termsFile = pkgs.writeText "one-piece-terms.tsv" (builtins.readFile ./data/one-piece-terms.tsv);
          ldLibraryPath = pkgs.lib.makeLibraryPath [
            pkgs.stdenv.cc.cc.lib
            pkgs.ffmpeg_7
            pkgs.zlib
            pkgs.zstd
            pkgs.rocmPackages.clr
            pkgs.rocmPackages.hipblas
            pkgs.rocmPackages.hiprand
            pkgs.rocmPackages.rocblas
            pkgs.rocmPackages.hipsparse
            pkgs.rocmPackages.hipsolver
            pkgs.rocmPackages.miopen
            pkgs.rocmPackages.rocm-runtime
          ];
          # Create NLTK data directory with required tokenizers
          nltkData = pkgs.runCommand "nltk-data" { buildInputs = [ pkgs.python3 ]; } ''
            mkdir -p $out/tokenizers
            python3 -c "import nltk; nltk.download('punkt', download_dir='$out')" 2>/dev/null || true
          '';
          pythonWithNltk = pkgs.python3.withPackages (ps: [ ps.nltk ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.coreutils
              pkgs.ffmpeg_7
              pkgs.gawk
              pkgs.gnused
              pkgs.mkvtoolnix
              pkgs.zlib
              pkgs.python3
              pythonWithNltk
              pkgs.stdenv.cc.cc.lib
              pkgs.rocmPackages.clr
              pkgs.rocmPackages.hipblas
              pkgs.rocmPackages.hiprand
              pkgs.rocmPackages.rocblas
              pkgs.rocmPackages.hipsparse
              pkgs.rocmPackages.hipsolver
              pkgs.rocmPackages.miopen
              pkgs.rocmPackages.rocm-runtime
              pkgs.uv
            ] ++ [ self.packages.${system}.default ];
            shellHook = ''
              export FUN_PACE_DEFAULT_TERMS_FILE="${termsFile}"
              export NLTK_DATA="${nltkData}:~/.nltk_data:/usr/share/nltk_data"
              if [ -n "''${LD_LIBRARY_PATH:-}" ]; then
                export LD_LIBRARY_PATH="${ldLibraryPath}:$LD_LIBRARY_PATH"
              else
                export LD_LIBRARY_PATH="${ldLibraryPath}"
              fi
            '';
          };
        });
    };
}
