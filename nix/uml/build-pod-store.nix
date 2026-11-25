# SPDX-License-Identifier: Apache-2.0

/*
  The test's settings of the build pod stores `kubenix/build-pods.nix` renders
  (phases/buildpods.py). nixidae/nixidae#1560, spike.

  The stores take only derivations that ask for the feature, so the phases'
  other builds stay on central. Central relays the pod's log, and the phase
  reads `build_one_inputs` there, an info event.
*/
{
  nixkube.buildPods = {
    systemFeatures = [ "nixkube-podfarm" ];
    mandatoryFeatures = [ "nixkube-podfarm" ];
    deadlineSeconds = 600;
    runner = true;
    # One CPU node: a small request, and the limit gives the build `NIX_BUILD_CORES=1`.
    runnerResources = {
      requests.cpu = "100m";
      limits.cpu = "1";
    };
    podSettings.log_level = "INFO";
  };
}
