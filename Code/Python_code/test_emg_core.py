import tempfile
import unittest
from pathlib import Path

from emg_core import (
    Calibration,
    coefficient_of_variation,
    correct_signal,
    create_calibration,
    observed_sample_rate,
    parse_adc_line,
    parse_sample_line,
    percent_mvc,
    rms,
)


class SignalProcessingTests(unittest.TestCase):
    def test_adc_parser_accepts_only_12_bit_integers(self):
        self.assertEqual(parse_adc_line(b"2048\r\n"), 2048)
        self.assertEqual(parse_adc_line("0"), 0)
        self.assertIsNone(parse_adc_line("4096"))
        self.assertIsNone(parse_adc_line("12.5"))
        self.assertIsNone(parse_adc_line(b"noise"))

    def test_multichannel_parser(self):
        self.assertEqual(
            parse_sample_line(b"100,200,300,400,500,600\r\n"),
            (100, 200, 300, 400, 500, 600),
        )
        self.assertEqual(parse_sample_line("123"), (123,))
        self.assertIsNone(parse_sample_line("1,2,3"))
        self.assertIsNone(parse_sample_line("1,2,3,4,5,4096"))

    def test_correction_clamps_below_baseline(self):
        self.assertEqual(correct_signal([90, 100, 140], 100), [0.0, 0.0, 40.0])

    def test_rms_and_percent_mvc(self):
        self.assertAlmostEqual(rms([3, 4]), 3.5355339)
        self.assertEqual(percent_mvc(25, 50), 50)
        self.assertEqual(percent_mvc(80, 50, cap=120), 120)
        self.assertEqual(percent_mvc(20, 0), 0)

    def test_observed_rate(self):
        self.assertAlmostEqual(observed_sample_rate([1.0, 1.001, 1.002]), 1000.0)

    def test_calibration_round_trip(self):
        calibration = create_calibration(
            "Biceps",
            [98, 100, 102, 100],
            [[200, 220, 240], [210, 230, 250], [205, 225, 245]],
            950.0,
        )
        self.assertGreater(calibration.mvc_rms_adc, 100)
        self.assertLess(coefficient_of_variation(calibration.mvc_trials), 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            calibration.save(path)
            loaded = Calibration.load(path)
        self.assertEqual(loaded.muscle, "Biceps")
        self.assertAlmostEqual(loaded.mvc_rms_adc, calibration.mvc_rms_adc)

    def test_calibration_rejects_no_contraction(self):
        with self.assertRaisesRegex(ValueError, "not clearly above"):
            create_calibration("Biceps", [100, 101, 99], [[100, 101], [101, 100]], 1000)

    def test_calibration_rejects_inconsistent_trials(self):
        with self.assertRaisesRegex(ValueError, "varied by"):
            create_calibration(
                "Biceps",
                [98, 100, 102, 100],
                [[120, 125, 130], [400, 420, 440], [180, 190, 200]],
                1000,
            )


if __name__ == "__main__":
    unittest.main()
