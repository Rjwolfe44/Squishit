from video_compressor.core.eta import ETACalculator


def test_eta_calculator_reports_progress_and_eta():
    calculator = ETACalculator(window_seconds=5.0, smoothing=0.5)

    first = calculator.update(10, 100, now=0.0)
    second = calculator.update(40, 100, now=3.0)

    assert first.progress_percent == 10.0
    assert second.progress_percent == 40.0
    assert second.units_per_second > 0
    assert second.eta_seconds > 0
