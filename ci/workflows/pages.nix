# The Pages workflow, as a value rather than as text.
#
# `.github/workflows/pages.yaml` is rendered from this file. Edit this one;
# `ciWorkflowCheck` compares the render against what is committed.
# `nix run --file . internal.ci-workflow-update` writes the new render.
#
# It is the one workflow of nixkube's own repository, which josh exports from
# nixidae (nixidae/nixidae#4). The tests run in nixidae, from
# ci/workflows/tests.nix.
{ lib, ghalib }:
let
  inherit (import ./bootstrap.nix { inherit lib; }) bootstrap;
  mainOnly = "github.ref == 'refs/heads/main'";
in
ghalib.evalWorkflow {
  name = "Pages";

  on = {
    push.branches = [ "main" ];
    workflow_dispatch = null;
  };

  jobs = {
    docs-build = {
      "if" = mainOnly;
      runs-on = "ubuntu-24.04";
      timeout-minutes = 30;
      # `nixkube` and not `lillecarl`: this is the cache the repository owns
      # and the one every other job pushes to. A push to `lillecarl`
      # answered 403 "You're not authorized to access binary cache
      # lillecarl."
      ghanix = lib.mkMerge [
        bootstrap
        {
          nix.cachix = {
            enable = true;
            name = "nixkube";
          };
        }
      ];
      steps = [
        {
          name = "Build documentation";
          run = "nix build --file . docs.default --out-link result --print-build-logs --print-out-paths";
        }
        (ghalib.steps.verifyClosure { name = "Verify docs closure"; })
        (ghalib.steps.preparePages { })
        (ghalib.steps.uploadPages { })
      ];
    };

    docs-deploy = {
      "if" = mainOnly;
      needs = "docs-build";
      runs-on = "ubuntu-24.04";
      timeout-minutes = 15;
      permissions = {
        pages = "write";
        id-token = "write";
      };
      environment = {
        name = "github-pages";
        url = "\${{ steps.deployment.outputs.page_url }}";
      };
      concurrency = {
        group = "pages";
        cancel-in-progress = false;
      };
      # `steps.deployment.outputs.page_url` above is what names the id.
      steps = [ (ghalib.steps.deployPages { }) ];
    };
  };
}
