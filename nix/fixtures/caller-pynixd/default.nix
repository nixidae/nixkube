# SPDX-License-Identifier: Apache-2.0

# Stands in for pynixd in `callerSourcesReachPynixd`: a caller's `sources`
# that names this directory must get this answer back.
{ ... }:
{
  packages.pynixd-lib = "caller-pynixd";
}
