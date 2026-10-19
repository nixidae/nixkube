# The test workflow, as a value rather than as text.
#
# nixidae renders it to `.github/workflows/nixkube-tests.yml`, with nixkube as
# the working directory of every step. Tests run only in nixidae; nixkube's
# own repository runs ci/workflows/pages.nix. nixidae/nixidae#4.
#
# ghanix is the schema. It takes `lib` and nothing else, which is what lets
# this repository use it while pinning its own nixpkgs. See issue #13.
#
{ lib, ghalib }:
let
  # The checkout, the Nix install and the runner permissions every job
  # needs, as `ghanix` options rather than as steps written out.
  inherit (import ./bootstrap.nix { inherit lib; }) bootstrap guest;

  # Publish from main and release tags only. The image tags are named from
  # versions, not from the commit, so a branch that changes neither version
  # would overwrite main's tag with its own content under the same name.
  #
  # A release tag here is `nixkube/v<version>`: every source of nixidae
  # shares one tag namespace. The release job tags `v<version>` in
  # nixidae/nixkube.
  publishRef = "github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/nixkube/v')";

  # Every attribute a job builds, checked for the architecture it is built on.
  #
  # `pushArch.aarch64-linux` is missing on purpose: it is foreign here and
  # native on the runner that builds it.
  #
  # `pushManifest` is the one most likely to regress. It names the two
  # architectures' images as plain strings today, so it costs nothing;
  # interpolating the derivations instead would read as a tidy-up and would
  # make `build-manifests` need an aarch64 machine.
  archAudited = [
    "push"
    "push-ci2"
    "push-citest"
    "kubenixApply.manifestJSONFile"
    "kubenixApply.manifestYAMLFile"
    "kubenixCI1.deploymentScript"
    "kubenixCI2.deploymentScript"
    "kubenixCITest.deploymentScript"
    "nixImage.pushArch.x86_64-linux"
    "nixImage.pushManifest"
    "nixImage.tagRelease"
    "scratchImage.push"
  ];

  # The two cluster tests, each on a guest that boots on the runner. The
  # test itself is nix/uml/ci.py, so a developer runs exactly what CI runs:
  # `nix run --file . ciTest.driver -- --out ./out`.
  #
  # `./out` holds the run's evidence: events.jsonl, junit.xml, report.json
  # and each guest's console and journal. It is uploaded whatever the
  # verdict, because a red run is the one somebody has to read.
  testQemu =
    { attr, what }:
    {
      needs = "build-manifests";
      runs-on = "ubuntu-latest";
      timeout-minutes = 60;
      ghanix = guest;
      steps = [
        {
          name = "Deploy and test ${what}, on a guest";
          run = "nix run --file . ${attr}.driver -- --out ./out";
        }
        (ghalib.steps.uploadArtifact {
          name = "Keep the run's evidence";
          artifactName = attr;
          path = "out";
        })
      ];
    };
in
# `${{ github.workflow }}` in the concurrency group below is this name, as
# nixidae renders it.
ghalib.evalWorkflow {
  name = "tests";
  on = {
    pull_request = null;
    push = {
      branches = [ "*" ];
      tags = [ "nixkube/v*" ];
    };
    workflow_dispatch = null;
  };

  # One run per ref. A push to a branch cancels the run the previous push
  # started, because that run is testing a commit nobody will deploy.
  #
  # Measured on 2026-09-09, after a day of small commits to develop: 22 runs
  # unfinished at once and 18 concurrent build-arm64 jobs, each rebuilding the
  # same 954 MiB aarch64 closure from source. None could help another, because
  # `push` only runs after its own build finishes. Every one of those was a
  # duplicate of the newest.
  #
  # Tags keep their runs. A release is not superseded by the next push, and
  # cancelling one would leave a version half published.
  concurrency = {
    group = "\${{ github.workflow }}-\${{ github.ref }}";
    cancel-in-progress = "\${{ !startsWith(github.ref, 'refs/tags/') }}";
  };

  env = {
    REPO_USERNAME = "\${{ github.actor }}";
    REPO_TOKEN = "\${{ secrets.GITHUB_TOKEN }}";
    CACHIX_AUTH_TOKEN = "\${{ secrets.CACHIX_AUTH_TOKEN }}";
  };

  permissions = {
    contents = "write";
    packages = "write";
    actions = "write";
  };

  jobs = {
    check = {
      runs-on = "ubuntu-latest";
      # A job with no `timeout-minutes` gets GitHub's default of 360, and a
      # build that hangs then holds a runner for six hours. That is what issue
      # #9 cost, twice per run. Every job here carries a bound for that reason.
      timeout-minutes = 30;
      ghanix = bootstrap;
      steps = [
        # `nix develop`, not `nix-shell --run`. To run a command, nix-shell
        # needs an interactive bash, and it gets one by evaluating its own
        # built-in `(import <nixpkgs> {}).bashInteractive`. Nothing in this
        # repository asks for that; nix-shell does. With no `nixpkgs` in
        # NIX_PATH it fails:
        #
        #   error: file 'nixpkgs' was not found in the Nix search path
        #   uses bash from your environment
        #
        # The derivation still came from shell.nix, so the type check was
        # correct, but the shell around it was the runner's rather than a
        # pinned one.
        {
          name = "Run type checker";
          run = "nix develop --file shell.nix --command pyright pkgs/nixkube/src";
        }
        # One step, and `nix build --file ./checks.nix all` is the same one a
        # contributor runs. Eight gates were eight steps here, which meant
        # the only list of what CI checks lived in this file -- so running it
        # locally meant reading the YAML and copying commands.
        #
        # `default.nix` says what each gate is for, beside the gate. Adding
        # one there and forgetting this file is now impossible, which is the
        # point. See issue #32.
        #
        # They are evaluation-only and their closures are almost empty, so
        # this costs seconds.
        {
          name = "Run the checks";
          run = "nix build --show-trace --file ./checks.nix all";
        }
        # A runner has no binfmt, so an attribute that needs the other
        # architecture cannot be built by any job here. A workstation with
        # extra-platforms emulates it and says nothing, which is how
        # kubenixApply came to need 3120 aarch64 derivations on x86_64.
        {
          name = "Check that every CI attribute builds natively";
          run = lib.concatStringsSep " \\\n  " ([ "nix run --file . arch-audit --" ] ++ archAudited) + "\n";
        }
        {
          name = "Check formatting";
          run = ''
            nix run --file . treefmt
            git diff --exit-code -- .
          '';
        }
      ];
    };

    build-amd64 = {
      runs-on = "ubuntu-latest";
      # Same reason as build-arm64 below, lower bound: this one has never been
      # the slow half.
      timeout-minutes = 45;
      ghanix = bootstrap;
      steps = [
        # --print-build-logs, because without it a stuck build is silent.
        #
        # Measured on run 34477979957: both builders printed their last line
        # within 20 seconds of starting, then said nothing for 44 and 60
        # minutes, until timeout-minutes killed them. `building '<drv>'...`
        # was all either one gave, so nothing said which phase was stuck. See
        # #21.
        {
          name = "Build and push environments";
          run = ''
            unlink nix-envs || true
            nix build --show-trace --print-build-logs --file . push --out-link nix-envs
            ./nix-envs/bin/push
          '';
        }
        # Built on every branch, published from main and tags only.
        # Building still proves it builds.
        {
          name = "Build amd64 nix image";
          run = ''
            unlink nix-nix || true
            nix build --show-trace --print-build-logs --file . nixImage.pushArch.x86_64-linux --out-link nix-nix
          '';
        }
        {
          name = "Push amd64 nix image";
          "if" = publishRef;
          run = "./nix-nix/bin/push-nix-x86_64-linux";
        }
      ];
    };

    build-arm64 = {
      runs-on = "ubuntu-24.04-arm";
      # Fail rather than hang. Measured on 2026-09-10: this job sat in "Build
      # and push environments" for three hours, and GitHub's default would
      # have let it reach six. `build-manifests` needs it, so everything
      # downstream -- both kind tests, the release -- waits behind a job that
      # is not progressing, and the whole run publishes nothing.
      #
      # It is not building from source when this happens. Measured the same
      # day: 3260 of 3265 paths in this job's aarch64 closure are already on a
      # substituter, and the five that are not came from unpushed commits. The
      # stall is in fetching, which is what issue #21 says.
      #
      # 90 minutes is well above a healthy run and well below the point where
      # a stuck job costs a day of pipeline.
      timeout-minutes = 90;
      ghanix = bootstrap;
      steps = [
        # One derivation at a time, with every core. This is insurance, not
        # the fix for what killed this job.
        #
        # `max-jobs auto` lets a 4-core, 16 GB runner build four of nixkube's
        # Python packages at once; grpclib-ttrpc compiles a Go ttrpc test
        # server and runs its suite, and four more run grpcio-tools. That
        # would be a plausible cause and is not this one: the job stalls while
        # *fetching* from cachix, before it builds anything. See issue #21 and
        # the note in ./bootstrap.nix.
        #
        # Kept because this runner has died twice, and the next path that
        # lands uncached will be built here rather than fetched.
        {
          name = "Build and push environments";
          run = ''
            unlink nix-envs || true
            nix build --show-trace --print-build-logs --max-jobs 1 --cores 4 --file . push --out-link nix-envs
            ./nix-envs/bin/push
          '';
        }
        # Built everywhere, published from main and tags only. See the
        # amd64 job above for why.
        {
          name = "Build arm64 nix image";
          run = ''
            unlink nix-nix || true
            nix build --show-trace --print-build-logs --max-jobs 1 --cores 4 --file . nixImage.pushArch.aarch64-linux --out-link nix-nix
          '';
        }
        {
          name = "Push arm64 nix image";
          "if" = publishRef;
          run = "./nix-nix/bin/push-nix-aarch64-linux";
        }
      ];
    };

    build-manifests = {
      needs = [
        "build-amd64"
        "build-arm64"
      ];
      runs-on = "ubuntu-latest";
      timeout-minutes = 30;
      ghanix = bootstrap;
      steps = [
        # Both build jobs have passed, so every store path the deployment
        # names should now be fetchable. Assert it before assembling the index
        # rather than after, because the index is what consumers pull.
        #
        # It walks the closure and unions the substituters. A per-path narinfo
        # check passes on the shape that actually bit: cacheEnv present on
        # cachix with a closure member only on cache.nixos.org, and issue #8's
        # present-top-path-absent-member.
        {
          name = "Assert both architectures' closures are fetchable";
          run = ''
            nix build --show-trace --file . kubenixApply.manifestJSONFile --out-link manifest
            nix run --file . assert-cached -- manifest
          '';
        }
        # These write floating tags -- `nix:latest`, `scratch:latest` and the
        # version tags -- and nothing here was branch-guarded, so a push to
        # any branch republished what consumers pull. It has not bitten only
        # because `build-arm64` stalls before this job runs.
        #
        # A branch still builds and still asserts. It just does not publish,
        # so the kind tests below run against the images main published.
        {
          name = "Build the index and scratch publishers";
          run = ''
            unlink nix-manifest || true
            unlink nix-scratches || true
            nix build --show-trace --file . nixImage.pushManifest --out-link nix-manifest
            nix build --show-trace --file . scratchImage.push --out-link nix-scratches
          '';
        }
        {
          name = "Create nix multi-arch manifest";
          "if" = publishRef;
          run = "./nix-manifest/bin/push-nix-manifest";
        }
        {
          name = "Push scratch image";
          "if" = publishRef;
          run = "./nix-scratches/bin/push";
        }
        # Let this fail the job. The cache is load-bearing, not an
        # optimisation: a node cannot bring itself up without nodeEnv, and a
        # node with no local store cannot build one. A push that silently does
        # nothing shows up days later as a pod stuck in ContainerCreating,
        # nowhere near this line.
        #
        # A fork has no token and cannot push. That case is skipped by name
        # here, rather than by discarding every error including the real ones.
        {
          name = "Push nocache variant store paths";
          "if" = "env.CACHIX_AUTH_TOKEN != ''";
          run = "nix run --file . push-ci2";
        }
        # The test workloads, for the same reason and with the same exception.
        # `env-ssl` runs a derivation built on this runner, and a node cannot
        # fetch what nothing published: it failed with "don't know how to
        # build these paths: ...-printer" on every kind run. See issue #30.
        {
          name = "Push test workload store paths";
          "if" = "env.CACHIX_AUTH_TOKEN != ''";
          run = "nix run --file . push-citest";
        }
      ];
    };

    /*
      The node test, as a virtual machine, on the runner itself.

      The same test and the same script a developer runs on a laptop:

          nix run --file . umlTest.driver -- --out ./uml-out

      `--out` is where the run's evidence goes: junit.xml with one case
      per chaos scenario, events.jsonl, each guest's console and journal.

      There is no Kind cluster and no registry here. The guest's store view
      comes from the runner's /nix/store, so everything the node needs is
      already on the machine that built it -- which is what lets this job
      run with no `needs` and no published image.

      `ubuntu-latest` because it is x64, `openKvm` because the runner user
      cannot open /dev/kvm without it, and `userNamespaces` because passt
      cannot start without one.
    */
    test-qemu = {
      runs-on = "ubuntu-latest";
      timeout-minutes = 60;
      ghanix = guest;
      steps = [
        {
          name = "Run the node test as a virtual machine";
          run = "nix run --file . umlTest.driver -- --out ./uml-out";
        }
        (ghalib.steps.uploadArtifact {
          name = "Keep the run's evidence";
          artifactName = "umlTest";
          path = "uml-out";
        })
      ];
    };

    # nixkube deployed through `kubenixDeploy` onto a kubeadm node, and the
    # Jobs of ci/test-jobs.nix run against it: the asserted ones finish, the
    # rejected ones do not, and deleting them cleans their volumes up. Once
    # without the pynixd cache and once with it.
    #
    # `needs = build-manifests` because the deployment names images on
    # ghcr.io and store paths on cachix, and a node cannot fetch what
    # nothing published.
    test-qemu-ci = testQemu {
      attr = "ciTest";
      what = "without the pynixd cache";
    };

    test-qemu-ci-cache = testQemu {
      attr = "ciTestCache";
      what = "with the pynixd cache";
    };

    /*
      Move `tested` in nixidae/nixkube to this commit's export, once every
      test passed on main. It is what a user targets: its images are
      published, because `build-manifests` ran first, and its manifest
      deployed. main can name a content tag that no job has pushed yet.

      The API rather than `git push`: the checkout persists the run's own
      token, which wins over one in a push URL. Without `force` the update
      only fast-forwards. It fails when the export of this commit has not
      reached nixidae/nixkube yet; the tests take longer than the export.
    */
    tested = {
      needs = [
        "check"
        "build-manifests"
        "test-qemu"
        "test-qemu-ci"
        "test-qemu-ci-cache"
      ];
      runs-on = "ubuntu-latest";
      "if" = "github.ref == 'refs/heads/main'";
      timeout-minutes = 30;
      ghanix = lib.mkMerge [
        bootstrap
        { checkout.fetchDepth = 0; }
      ];
      steps = [
        {
          id = "exported";
          name = "Find nixkube's own commit";
          run = ''echo "commit=$(nix shell --file .. josh --command josh-filter :/nixkube "$GITHUB_SHA")" >> "$GITHUB_OUTPUT"'';
        }
        {
          name = "Advance tested, fast-forward only";
          env = {
            GH_TOKEN = "\${{ secrets.JOSH_EXPORT_TOKEN }}";
            COMMIT = "\${{ steps.exported.outputs.commit }}";
          };
          run = ''
            if gh api repos/nixidae/nixkube/git/ref/heads/tested >/dev/null 2>&1; then
              gh api --method PATCH repos/nixidae/nixkube/git/refs/heads/tested -f sha="$COMMIT" -F force=false
            else
              gh api --method POST repos/nixidae/nixkube/git/refs -f ref=refs/heads/tested -f sha="$COMMIT"
            fi
          '';
        }
      ];
    };

    release = {
      needs = [
        "check"
        "build-manifests"
        "test-qemu-ci"
        "test-qemu-ci-cache"
      ];
      runs-on = "ubuntu-latest";
      "if" = "startsWith(github.ref, 'refs/tags/nixkube/v')";
      timeout-minutes = 30;
      # The whole history, because josh filters all of it.
      ghanix = lib.mkMerge [
        bootstrap
        { checkout.fetchDepth = 0; }
      ];
      steps = [
        # `build-manifests` published this commit's content tags. The
        # release names the index by digest, so the image under a released
        # manifest cannot change (#825); the version tags are for people.
        {
          id = "digest";
          name = "Tag the release's image, and find its digest";
          run = ''echo "digest=$(nix run --file . nixImage.tagRelease)" >> "$GITHUB_OUTPUT"'';
        }
        # A release YAML nobody has checked is deployable is how #3 and #4
        # reached users. Every path it names has to be fetchable, because a
        # node can only substitute them.
        {
          name = "Render deployment manifest";
          env.DIGEST = "\${{ steps.digest.outputs.digest }}";
          run = ''
            nix build --show-trace --file . --argstr releaseDigest "$DIGEST" kubenixRelease.manifestYAMLFile
            cp result nix-csi-deployment.yaml
          '';
        }
        {
          name = "Assert the release manifest is deployable";
          run = "nix run --file . assert-cached -- nix-csi-deployment.yaml";
        }
        # **The release lives in nixidae/nixkube, where its users look.**
        # The tag there is `v<version>`, on the commit that repository holds
        # for this one: josh at the version nixidae pins gives the same hash
        # the export pushed. The export runs on the push to main, so a tag on
        # a commit of main names a commit that repository already has.
        {
          id = "exported";
          name = "Find nixkube's own commit and tag";
          env.TAG = "\${{ github.ref_name }}";
          run = ''
            echo "commit=$(nix shell --file .. josh --command josh-filter :/nixkube "$GITHUB_SHA")" >> "$GITHUB_OUTPUT"
            echo "tag=''${TAG#nixkube/}" >> "$GITHUB_OUTPUT"
          '';
        }
        {
          name = "Create GitHub Release";
          uses = "softprops/action-gh-release@v1";
          "with" = {
            repository = "nixidae/nixkube";
            token = "\${{ secrets.JOSH_EXPORT_TOKEN }}";
            tag_name = "\${{ steps.exported.outputs.tag }}";
            target_commitish = "\${{ steps.exported.outputs.commit }}";
            files = "nix-csi-deployment.yaml";
            draft = false;
            prerelease = false;
          };
        }
      ];
    };
  };
}
