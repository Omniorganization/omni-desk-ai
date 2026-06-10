from omnidesk_agent.learning.failure_analyzer import FailureAnalyzer


def test_failure_analyzer_classifies_common_failures():
    analyzer = FailureAnalyzer()
    assert analyzer.classify(error="No module named pytest") == "missing_dependency"
    assert analyzer.classify(error="captcha required") == "captcha_required"
    assert analyzer.classify(error="element not found for selector") == "selector_changed"
