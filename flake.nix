{
  description = "ChatGPT history source plugin for contextualize";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forSystems = nixpkgs.lib.genAttrs systems;
    in {
      packages = forSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          plugin = pkgs.python312Packages.buildPythonPackage {
            pname = "cx-cgpt";
            version = "0.1.0";
            pyproject = true;
            src = self;
            build-system = [ pkgs.python312Packages.hatchling ];
            dependencies = [ pkgs.python312Packages.click ];
            nativeCheckInputs = [ pkgs.python312Packages.pytestCheckHook ];
            pythonImportsCheck = [ "cx_cgpt.plugin" ];
          };
        in {
          default = plugin;
          cx-cgpt = plugin;
        });
      checks = forSystems (system: { default = self.packages.${system}.default; });
      devShells = forSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          default = pkgs.mkShell {
            packages = [ pkgs.python312 pkgs.uv ];
            env.UV_PYTHON = pkgs.python312.interpreter;
            env.UV_PYTHON_DOWNLOADS = "never";
          };
        });
    };
}
