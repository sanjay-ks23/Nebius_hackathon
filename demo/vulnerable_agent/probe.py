"""StopProof observability shim. Emits structured events when run inside a StopProof sandbox."""
try:
    from stopproof.runtime import emit
except ImportError:  # running outside a StopProof sandbox

    def emit(event, action_id=None, **data):
        return None
