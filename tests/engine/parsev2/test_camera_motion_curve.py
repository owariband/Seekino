import json
import math
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[3] / "engine" / "parsev2" / "viewport-v1.mjs"
# The runtime uses Abramowitz and Stegun 7.1.26, whose erf error is at most 1.5e-7.
# After normalising the curve that is a few 1e-6 of progress and 1e-6 relative speed.
PROGRESS_TOLERANCE = 5e-6  # unitless progress in [0, 1]
VELOCITY_RELATIVE_TOLERANCE = 2e-5  # progress per millisecond
VELOCITY_ABSOLUTE_TOLERANCE = 1e-9  # progress per millisecond, for near-zero speeds


def _run_node(script: str, payload: object) -> str:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(payload)],
        cwd=MODULE.parent, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _reference(elapsed_ms: float, duration_ms: float, sigma: float) -> tuple[float, float]:
    """The authored curve with math.erf; returns progress and progress per millisecond."""
    if elapsed_ms <= 0:
        return 0.0, 0.0
    if elapsed_ms >= duration_ms:
        return 1.0, 0.0
    u = elapsed_ms / duration_ms
    scale = sigma * math.sqrt(math.pi / 2)
    spread = math.sqrt(2) * sigma
    baseline = math.exp(-0.5 * (0.5 / sigma) ** 2)

    def integral(x: float) -> float:
        return scale * (math.erf((x - 0.5) / spread) + math.erf(0.5 / spread))

    total = integral(1) - baseline
    progress = min(1.0, max(0.0, (integral(u) - baseline * u) / total))
    velocity = (math.exp(-0.5 * ((u - 0.5) / sigma) ** 2) - baseline) / total / duration_ms
    return progress, velocity


def test_gaussian_motion_tracks_the_exact_erf_expression_across_widths_and_durations():
    cases = [
        (duration_ms * fraction, duration_ms, sigma)
        for duration_ms in (1000, 9000)
        for sigma in (0.05, 0.08, 0.18, 0.35, 0.5)
        for fraction in (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95)
    ]
    script = """
      import { gaussianMotionAt } from './viewport-v1.mjs';
      const cases = JSON.parse(process.argv[1]);
      console.log(JSON.stringify(cases.map(([at, duration, sigma]) => gaussianMotionAt(at, duration, sigma))));
    """
    computed = json.loads(_run_node(script, cases))
    assert len(computed) == len(cases)
    for case, motion in zip(cases, computed):
        expected = _reference(*case)
        assert set(motion) == {"progress", "velocity_per_ms"}, case
        assert motion["progress"] == pytest.approx(expected[0], abs=PROGRESS_TOLERANCE), case
        assert motion["velocity_per_ms"] == pytest.approx(
            expected[1], rel=VELOCITY_RELATIVE_TOLERANCE, abs=VELOCITY_ABSOLUTE_TOLERANCE), case


def test_gaussian_motion_is_monotone_symmetric_and_centred():
    script = """
      import assert from 'node:assert/strict';
      import { gaussianMotionAt } from './viewport-v1.mjs';

      const duration = 4000;
      const peaks = [];
      for (const sigma of [0.05, 0.1, 0.18, 0.35, 0.5]) {
        const motion = at => gaussianMotionAt(at, duration, sigma);
        const samples = Array.from({length: 401}, (_, i) => motion(duration * i / 400));
        // Exact authored endpoints, with zero speed outside the shot as well.
        for (const at of [-1, 0]) assert.deepEqual(motion(at), {progress: 0, velocity_per_ms: 0});
        for (const at of [duration, duration + 1]) {
          assert.deepEqual(motion(at), {progress: 1, velocity_per_ms: 0});
        }
        // Dense travel never reverses.
        assert.ok(samples.every((s, i) => i === 0 || s.progress >= samples[i - 1].progress));
        // Symmetry about the midpoint in both progress and speed.
        for (let i = 0; i <= 200; i++) {
          const left = motion(duration * i / 400);
          const right = motion(duration * (400 - i) / 400);
          assert.ok(Math.abs(left.progress + right.progress - 1) < 1e-9);
          assert.ok(Math.abs(left.velocity_per_ms - right.velocity_per_ms) < 1e-12);
        }
        // The positive speed maximum sits on the midpoint.
        const speeds = samples.map(sample => sample.velocity_per_ms);
        const peak = Math.max(...speeds);
        assert.ok(peak > 0);
        assert.equal(speeds[200], peak);
        assert.ok(speeds.every((speed, i) => i === 200 || speed < peak));
        peaks.push(peak * duration);
      }
      // A narrower sigma gives a taller, more concentrated move.
      assert.ok(peaks.every((peak, i) => i === 0 || peak < peaks[i - 1]));
      const atQuarter = sigma => gaussianMotionAt(duration / 4, duration, sigma).progress;
      assert.ok(atQuarter(0.05) < atQuarter(0.18) && atQuarter(0.18) < atQuarter(0.5));
    """
    _run_node(script, {})


def test_camera_at_prefers_an_explicit_gaussian_over_measured_progress_samples():
    script = """
      import assert from 'node:assert/strict';
      import { cameraAt, gaussianMotionAt } from './viewport-v1.mjs';

      const world = {width: 2000, height: 1000}, viewport = {width: 1000, height: 1000};
      const base = {duration_ms: 4000, easing: 'EASE_IN_OUT', from: [0, 0.5, 1], to: [1, 0.5, 1]};
      const samples = [[0, 0], [0.25, 0.1], [0.5, 0.3], [0.75, 0.6], [1, 1]];
      const sampled = cameraAt(1000, {...base, progress_samples: samples}, world, viewport);
      assert.ok(Math.abs(sampled.x - 100) < 1e-9);
      // The authored curve wins over the measured samples and honours an explicit sigma.
      const authored = cameraAt(1000, {...base, progress_samples: samples,
        velocity_curve: {kind: 'gaussian', sigma: 0.1}}, world, viewport);
      assert.ok(Math.abs(authored.x - 1000 * gaussianMotionAt(1000, 4000, 0.1).progress) < 1e-9);
      assert.ok(authored.x < sampled.x);
      const fallback = cameraAt(1000, {...base, velocity_curve: {kind: 'gaussian'}}, world, viewport);
      assert.ok(Math.abs(fallback.x - 1000 * gaussianMotionAt(1000, 4000).progress) < 1e-9);
      assert.deepEqual(Object.keys(authored).sort(), ['height', 'width', 'x', 'y']);
      assert.equal(cameraAt(0, {...base, velocity_curve: {kind: 'gaussian'}}, world, viewport).x, 0);
      assert.equal(cameraAt(4000, {...base, velocity_curve: {kind: 'gaussian'}}, world, viewport).x, 1000);
    """
    _run_node(script, {})


def test_camera_at_without_a_velocity_curve_keeps_easing_and_sampled_interpolation():
    script = """
      import assert from 'node:assert/strict';
      import { cameraAt } from './viewport-v1.mjs';

      const world = {width: 2000, height: 1000}, viewport = {width: 1000, height: 1000};
      const base = {duration_ms: 4000, from: [0, 0.5, 1], to: [1, 0.5, 1]};
      const steady = at => cameraAt(at, {...base, easing: 'STEADY'}, world, viewport);
      assert.equal(steady(0).x, 0);
      assert.equal(steady(4000).x, 1000);
      assert.ok(Math.abs(steady(1000).x - 250) < 1e-9);
      const eased = at => cameraAt(at, {...base, easing: 'EASE_IN_OUT'}, world, viewport);
      assert.ok(Math.abs(eased(2000).x - 500) < 1e-9);
      assert.ok(eased(2000).x - eased(1999).x > eased(3999).x - eased(3998).x);

      const samples = [[0, 0], [0.25, 0.1], [0.5, 0.3], [0.75, 0.6], [1, 1]];
      const sampled = at => cameraAt(at, {...base, easing: 'EASE_IN_OUT', progress_samples: samples},
        world, viewport);
      assert.equal(sampled(0).x, 0);
      assert.equal(sampled(4000).x, 1000);
      for (const [at, x] of [[1000, 100], [2000, 300], [3000, 600]]) {
        assert.ok(Math.abs(sampled(at).x - x) < 1e-9);
      }
      const positions = Array.from({length: 121}, (_, i) => sampled(4000 * i / 120).x);
      assert.ok(positions.slice(1).every((x, i) => x >= positions[i]));
    """
    _run_node(script, {})


def test_gaussian_motion_and_camera_at_reject_invalid_parameters():
    script = """
      import assert from 'node:assert/strict';
      import { cameraAt, gaussianMotionAt } from './viewport-v1.mjs';

      const rejects = fn => assert.throws(fn, RangeError);
      for (const sigma of [-0.18, 0.049, 0.501, NaN, Infinity]) {
        rejects(() => gaussianMotionAt(500, 1000, sigma));
      }
      for (const duration of [0, -1000, NaN, Infinity]) {
        rejects(() => gaussianMotionAt(500, duration));
      }
      for (const at of [NaN, Infinity, -Infinity, undefined]) {
        rejects(() => gaussianMotionAt(at, 1000));
      }
      // Inclusive sigma bounds remain valid, and sigma defaults to 0.18.
      assert.ok(gaussianMotionAt(500, 1000, 0.05).progress > 0);
      assert.ok(gaussianMotionAt(500, 1000, 0.5).progress > 0);
      assert.equal(gaussianMotionAt(500, 1000).progress, gaussianMotionAt(500, 1000, 0.18).progress);

      const world = {width: 2000, height: 1000}, viewport = {width: 1000, height: 1000};
      const base = {duration_ms: 4000, easing: 'STEADY', from: [0, 0.5, 1], to: [1, 0.5, 1]};
      const camera = curve => () => cameraAt(1000, {...base, velocity_curve: curve}, world, viewport);
      rejects(camera({kind: 'linear'}));
      rejects(camera({}));
      rejects(camera(null));
      rejects(camera({kind: 'gaussian', sigma: 0.01}));
      rejects(camera({kind: 'gaussian', sigma: NaN}));
      rejects(() => cameraAt(NaN, {...base, velocity_curve: {kind: 'gaussian'}}, world, viewport));
      rejects(() => cameraAt(1000, {...base, duration_ms: 0, velocity_curve: {kind: 'gaussian'}},
        world, viewport));
    """
    _run_node(script, {})
