

def test_model_calls_wait_at_the_configured_gate():
    """`llm_max_concurrent` was configured and consulted only by an async limiter around candidates.

    Every real model call goes through `model.ask` on a pipeline worker thread, which never waited.
    Twenty-six candidate workers each asking two or three times put twenty-six concurrent requests on
    one gateway, and the degraded gateway that followed was the pipeline's largest single loss:
    forty-four of seventy package attempts refused with `the gateway did not answer: timed out` while
    a one-word prompt sent by hand came back in three seconds.
    """
    import threading
    from frf.core import model, rate_limiter

    rate_limiter.configure(max_concurrent=1, calls_per_minute=0)
    gate = rate_limiter.sync_gate()
    assert gate is not None, "configure() must build the gate the sync path waits at"

    inside = threading.Event()
    released = threading.Event()

    def hold():
        with gate:
            inside.set()
            released.wait(timeout=5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert inside.wait(timeout=5)

    entered = threading.Event()
    threading.Thread(target=lambda: (gate.__enter__(), entered.set()), daemon=True).start()
    assert not entered.wait(timeout=0.5), "a second caller entered a gate of one"
    released.set()
    holder.join(timeout=5)
    assert entered.wait(timeout=5), "and was let in once the first left"

    assert callable(getattr(model, "_sync_gate", None)), "ask() must be able to find the gate"
