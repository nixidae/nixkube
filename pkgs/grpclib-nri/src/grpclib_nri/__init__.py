"""grpclib_nri — NRI protocol utilities built on grpclib-ttrpc.

Provides NRI-specific protocol handling like multiplexing over a single socket,
a high-level NriServer class for running plugins, and an NriPlugin base class
that handles protocol boilerplate (Configure, Synchronize, Shutdown, stub handlers).
"""

from .mux import (
    PLUGIN_SERVICE_CONN as PLUGIN_SERVICE_CONN,
    RUNTIME_SERVICE_CONN as RUNTIME_SERVICE_CONN,
    MuxChannelTransport as MuxChannelTransport,
    NriMux as NriMux,
)
from .plugin import NriPlugin as NriPlugin, make_event_bitmask as make_event_bitmask
from .server import NriServer as NriServer
