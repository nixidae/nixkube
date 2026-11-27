# The node's store servers (`nixkube.node.storeServer`, nixidae/nixidae#1661),
# each as `ciTest` with a change to the deployment.
#
# ../default.nix makes each one `internal.ciStoreServer.<name>`, and
# ../ci/workflows/tests.nix makes each one a CI job, so a server listed here
# cannot miss CI. `nix-daemon` is `ciTest` itself.
#
# `subnet` is the guest LAN, 10.<subnet>.0.0/24: unique across nixidae's
# guest tests, because two runs at once share the host.
{
  # pynixd over a private Nix daemon, on nixkube's own store.
  pynixd = {
    subnet = 110;
    nixkube.node.storeServer = "pynixd";
  };
  # pynixd alone, on nixkube's own store.
  pynixd-daemonless = {
    subnet = 112;
    nixkube.node.storeServer = "pynixd-daemonless";
  };
  # pynixd alone, on the host's /nix beside the host's daemon: sg1's shape.
  pynixd-daemonless-hoststore = {
    subnet = 115;
    nixkube = {
      hostStore.enable = true;
      node.storeServer = "pynixd-daemonless";
    };
  };
}
