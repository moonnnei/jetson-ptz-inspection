"""Offline detector IPC and latency-contract checks; no hardware/model runs."""
import copy
import importlib.util
import io
from pathlib import Path
import struct
import unittest

PATH = Path(__file__).resolve().parents[2] / 'tools/validation/benchmark_detector.py'
SPEC = importlib.util.spec_from_file_location('detector_benchmark', str(PATH))
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class DetectorProtocolTests(unittest.TestCase):
    def result(self, flow=True, preview=True, held=False):
        return dict(decode_ms=2, preprocess_ms=3, network_ms=4, flow_ms=5,
                    postprocess_ms=6, preview_ms=7 if preview else 0, inference_ms=7,
                    control_result_ms=20, preview_generated=preview,
                    flow_reference_held=held,
                    total_process_ms=27 if preview else 20, wall_ms=30, flow_enabled=flow,
                    shift=[0, 0], shift_response=0,
                    persons=[dict(confidence=0.8, bbox=[0.1, 0.2, 0.4, 0.7])])

    def test_legacy_header_keeps_plain_jpeg_length(self):
        jpeg = b'\xff\xd8abcd\xff\xd9'
        payload = BENCH.request_bytes(jpeg, True)
        self.assertEqual(struct.unpack('<I', payload[:4])[0], len(jpeg))
        self.assertEqual(payload[4:], jpeg)

    def test_light_header_only_changes_bit31(self):
        jpeg = b'\xff\xd8abcd\xff\xd9'
        payload = BENCH.request_bytes(jpeg, False)
        header = struct.unpack('<I', payload[:4])[0]
        self.assertEqual(header & BENCH.NO_FLOW, BENCH.NO_FLOW)
        self.assertEqual(header & 0x7fffffff, len(jpeg))
        self.assertEqual(payload[4:], jpeg)

    def test_all_flag_combinations_keep_original_jpeg_length(self):
        jpeg = b'\xff\xd8abcd\xff\xd9'
        for flow in (True, False):
            for preview in (True, False):
                payload = BENCH.request_bytes(jpeg, flow, preview)
                header = struct.unpack('<I', payload[:4])[0]
                self.assertEqual(header & BENCH.LENGTH_MASK, len(jpeg))
                self.assertEqual(bool(header & BENCH.NO_FLOW), not flow)
                self.assertEqual(bool(header & BENCH.NO_PREVIEW), not preview)
                self.assertEqual(payload[4:], jpeg)

    def test_held_reference_flag_keeps_jpeg_and_other_flags(self):
        jpeg = b'\xff\xd8abcd\xff\xd9'
        for preview in (True, False):
            payload = BENCH.request_bytes(jpeg, True, preview, True)
            header = struct.unpack('<I', payload[:4])[0]
            self.assertEqual(header & BENCH.LENGTH_MASK, len(jpeg))
            self.assertEqual(header & BENCH.NO_FLOW, 0)
            self.assertEqual(bool(header & BENCH.NO_PREVIEW), not preview)
            self.assertEqual(header & BENCH.HOLD_FLOW_REFERENCE, BENCH.HOLD_FLOW_REFERENCE)
            self.assertEqual(payload[4:], jpeg)

    def test_holding_reference_without_flow_rejected_before_send(self):
        for preview in (True, False):
            with self.assertRaisesRegex(ValueError, 'requires optical flow'):
                BENCH.request_bytes(b'\xff\xd8abcd\xff\xd9', False, preview, True)

    def test_held_reference_response_flag_required(self):
        for preview in (True, False):
            BENCH.validate_result(self.result(True, preview, True), True, preview, True)
        bad = self.result()
        with self.assertRaisesRegex(ValueError, 'held reference flag'):
            BENCH.validate_result(bad, True, True, True)
        del bad['flow_reference_held']
        with self.assertRaisesRegex(ValueError, 'held reference flag'):
            BENCH.validate_result(bad, True)
        with self.assertRaisesRegex(ValueError, 'requires optical flow'):
            BENCH.validate_result(self.result(False, True, True), False, True, True)

    def test_empty_preview_does_not_consume_following_response(self):
        stream = io.BytesIO(b'next-response')
        self.assertEqual(BENCH.read_preview(stream, 0, False), b'')
        self.assertEqual(stream.read(), b'next-response')

    def test_empty_preview_only_allowed_when_requested(self):
        with self.assertRaisesRegex(RuntimeError, 'length'):
            BENCH.read_preview(io.BytesIO(b''), 0, True)
        with self.assertRaisesRegex(RuntimeError, 'unrequested'):
            BENCH.read_preview(io.BytesIO(b'\xff\xd8\xff\xd9'), 4, False)

    def test_enabled_preview_must_be_complete_jpeg(self):
        jpeg = b'\xff\xd8abcd\xff\xd9'
        self.assertEqual(BENCH.read_preview(io.BytesIO(jpeg), len(jpeg), True), jpeg)
        with self.assertRaisesRegex(RuntimeError, 'JPEG'):
            BENCH.read_preview(io.BytesIO(b'abcdefgh'), 8, True)

    def test_invalid_jpeg_length_rejected_before_send(self):
        for jpeg in (b'', b'abc', bytes(BENCH.MAX_JPEG + 1)):
            with self.assertRaises(ValueError):
                BENCH.request_bytes(jpeg, False)

    def test_truncated_response_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            BENCH.read_exact(io.BytesIO(b'ab'), 4)

    def test_partial_reads_reassembled(self):
        class ShortReads(io.BytesIO):
            def read(self, count):
                return super().read(min(count, 2))
        self.assertEqual(BENCH.read_exact(ShortReads(b'abcdef'), 6), b'abcdef')

    def test_legacy_inference_meaning_and_total(self):
        BENCH.validate_result(self.result(), True)
        BENCH.validate_result(self.result(False), False)
        bad = self.result()
        bad['inference_ms'] = bad['network_ms']
        with self.assertRaisesRegex(ValueError, 'meaning'):
            BENCH.validate_result(bad, True)
        bad = self.result()
        bad['total_process_ms'] -= bad['flow_ms']
        with self.assertRaisesRegex(ValueError, 'add up'):
            BENCH.validate_result(bad, True)

    def test_control_timing_and_skipped_preview_contract(self):
        for flow in (True, False):
            for preview in (True, False):
                BENCH.validate_result(self.result(flow, preview), flow, preview)
        bad = self.result()
        bad['control_result_ms'] = bad['total_process_ms']
        with self.assertRaisesRegex(ValueError, 'Control timing'):
            BENCH.validate_result(bad, True)
        bad = self.result(True, False)
        bad['preview_ms'] = 1
        bad['total_process_ms'] += 1
        with self.assertRaisesRegex(ValueError, 'Skipped preview'):
            BENCH.validate_result(bad, True, False)

    def test_old_binary_cannot_silently_skip_preview(self):
        bad = self.result()
        del bad['preview_generated']
        with self.assertRaisesRegex(ValueError, 'preview flag'):
            BENCH.validate_result(bad, True)
        with self.assertRaisesRegex(ValueError, 'preview flag'):
            BENCH.validate_result(self.result(), True, False)

    def test_nonfinite_and_missing_timing_rejected(self):
        for name in BENCH.METRICS:
            bad = self.result()
            bad[name] = float('nan')
            with self.assertRaises(ValueError):
                BENCH.validate_result(bad, True)
            bad = self.result()
            del bad[name]
            with self.assertRaises(ValueError):
                BENCH.validate_result(bad, True)

    def test_stale_flow_after_light_request_rejected(self):
        for key, value in (('shift', [0.02, 0]), ('shift_response', 0.7)):
            bad = self.result(False)
            bad[key] = value
            with self.assertRaisesRegex(ValueError, 'stale'):
                BENCH.validate_result(bad, False)

    def test_old_binary_cannot_silently_supply_new_metrics(self):
        bad = self.result()
        del bad['flow_enabled']
        with self.assertRaisesRegex(ValueError, 'flow flag'):
            BENCH.validate_result(bad, True)

    def test_person_coordinate_and_confidence_limits(self):
        for key, value in (('bbox', [0.1, 0.2, 1.1, 0.8]),
                           ('bbox', [0.5, 0.2, 0.3, 0.8]),
                           ('confidence', float('inf'))):
            bad = self.result()
            bad['persons'][0][key] = value
            with self.assertRaises(ValueError):
                BENCH.validate_result(bad, True)

    def test_output_comparison_detects_box_or_score_changes(self):
        original = self.result()['persons']
        self.assertTrue(BENCH.same_persons(original, copy.deepcopy(original)))
        self.assertFalse(BENCH.same_persons(original, []))
        for key, value in (('bbox', [0.2, 0.2, 0.4, 0.7]), ('confidence', 0.9)):
            modified = copy.deepcopy(original)
            modified[0][key] = value
            self.assertFalse(BENCH.same_persons(original, modified))

    def test_summary_excludes_warmup_and_keeps_wall_separate(self):
        normal = self.result()
        normal.update(mode='light', warmup=False)
        warm = self.result()
        warm.update(mode='light', warmup=True, wall_ms=300)
        summary = BENCH.summarize([normal, warm])['light']
        self.assertEqual(summary['count'], 1)
        self.assertEqual(summary['metrics']['wall_ms']['median'], 30)
        self.assertEqual(summary['metrics']['inference_ms']['median'], 7)
        self.assertEqual(summary['metrics']['total_process_ms']['median'], 27)


if __name__ == '__main__':
    unittest.main()
