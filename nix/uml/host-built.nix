# SPDX-License-Identifier: Apache-2.0

# What makes a node's system from the git source a path no cache holds:
# pynixd has to build it. The harness and the source repository both import
# this file, so they compute the same toplevel.
{
  environment.etc."nixkube-host-generation".text = "built\n";
}
