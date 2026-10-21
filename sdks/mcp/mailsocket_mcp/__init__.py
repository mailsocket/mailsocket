"""mailsocket MCP server — expose inboxes + the OTP wait to AI agents.

The actual server lives in :mod:`mailsocket_mcp.server` (imported as a
submodule, not re-exported under its own name, so ``mailsocket_mcp.server``
always resolves to the module for ``mailsocket_mcp.server:main``).
"""

from .server import MAX_WAIT_TIMEOUT, MISSING_KEY_MESSAGE, REMOTE_MAX_WAIT_TIMEOUT, __version__

__all__ = ["MAX_WAIT_TIMEOUT", "MISSING_KEY_MESSAGE", "REMOTE_MAX_WAIT_TIMEOUT", "__version__"]
