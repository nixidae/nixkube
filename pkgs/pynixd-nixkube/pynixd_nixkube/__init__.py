"""pynixd-nixkube"""

import warnings

# kr8s warns on every client against an apiserver newer than its hardcoded
# range (0.20.15 stops at 1.35), and has no setting to turn the check off.
warnings.filterwarnings("ignore", message=r"Kubernetes version .* is not supported")
