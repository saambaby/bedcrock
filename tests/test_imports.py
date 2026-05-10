def test_score_breakdown_canonical_module():
    from src.schemas import ScoreBreakdown
    assert ScoreBreakdown.__module__ == "src.schemas"


def test_no_duplicate_signal_module():
    import importlib.util
    assert importlib.util.find_spec("src.schemas.signal") is None


def test_no_duplicate_order_module():
    import importlib.util
    assert importlib.util.find_spec("src.schemas.order") is None
