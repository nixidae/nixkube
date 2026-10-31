# The remote store that nixidae's CI builds on and substitutes from: pynixd on
# dynhetz, croshome `hosts/dynhetz/pynixd.nix`. A ghanix `remoteBuilder`.
#
# The runner keeps its memory for guests and test drivers. A run by anyone
# else, or with no secret, builds on the runner alone.
{
  enable = true;
  uri = "ssh-ng://gha@37.27.129.237:2234";
  # `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAtGLJv5RjzUuOGWnsaTREsnAj6xVppAnegaXWfkJHHl`,
  # /var/lib/pynixd-ssh/ssh_host_ed25519_key.pub on dynhetz.
  hostKey = "c3NoLWVkMjU1MTkgQUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUF0R0xKdjVSanpVdU9HV25zYVRSRXNuQWo2eFZwcEFuZWdhWFdma0pISGw=";
  secret = "PYNIXD_BUILDER_KEY";
  actors = [ "Lillecarl" ];
  # `nix config show system-features` on dynhetz.
  supportedFeatures = [
    "benchmark"
    "big-parallel"
    "kvm"
    "nixos-test"
    "recursive-nix"
    "uid-range"
  ];
}
