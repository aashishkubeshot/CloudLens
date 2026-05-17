"""CloudLens — Cloud Run observability MCP + TUI."""

from __future__ import annotations

import os


# Silence the C-libraries used by google-cloud-* before anything in this
# package imports gRPC. They write directly to fd 2 with their own logging
# (`I0512 18:59:42 ev_poll_posix.cc:593] ...`) which corrupts the Textual
# rendering when the TUI is up. These must be set BEFORE the first
# `from google.cloud ...` import or they're ignored.
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GRPC_TRACE", "")
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("ABSL_LOG_THRESHOLD", "2")


__version__ = "0.5.1"
