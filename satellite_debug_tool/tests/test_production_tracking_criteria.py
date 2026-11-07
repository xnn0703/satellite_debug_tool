"""M54 固定窗口中的搜星、瞬降、恢复确认和未知证据边界。"""

from dataclasses import replace
import math

import pytest

from satellite_debug_tool.core.production.tracking_criteria import (
    TrackingCriteriaPolicy, TrackingEvaluator,
)


def policy(**changes):
    return replace(TrackingCriteriaPolicy(), acquisition_stable_s=1,
                   acquisition_timeout_s=3, recovery_stable_s=1,
                   interruption_ratio_max=0.5, **changes)


def evaluator(*, rules=None, duration=10, acquisition=True):
    value = TrackingEvaluator(rules or policy(), duration, acquisition)
    value.start(0)
    return value


def sample(value, at, *, snr=12, phase=True, locked=True, valid=True):
    value.observe(at, phase, locked, snr, valid)


def healthy_until(value, first, last, step=0.5):
    at = first
    while at <= last:
        sample(value, at)
        at += step


def test_defaults_and_mapping_round_trip():
    default = TrackingCriteriaPolicy.from_mapping()
    assert default.acquisition_timeout_s == 300
    assert default.to_payload() == {
        'acquisition_stable_s': 5.0, 'acquisition_timeout_s': 300.0,
        'interruption_max_s': 2.0, 'interruptions_per_30min': 5,
        'interruption_ratio_max': 0.005, 'recovery_stable_s': 3.0,
        'maximum_sample_gap_s': 2.0, 'attitude_error_max_deg': 3.0,
    }
    assert TrackingCriteriaPolicy.from_mapping(default.to_payload()) == default


@pytest.mark.parametrize('name,value', [
    ('acquisition_stable_s', 0), ('acquisition_timeout_s', -1),
    ('interruption_max_s', math.inf), ('interruptions_per_30min', 1.5),
    ('interruption_ratio_max', 1.1), ('recovery_stable_s', math.nan),
    ('maximum_sample_gap_s', True),
])
def test_policy_rejects_invalid_thresholds(name, value):
    with pytest.raises(ValueError):
        TrackingCriteriaPolicy.from_mapping({name: value})


def test_short_recipe_requires_explicitly_revised_timing():
    with pytest.raises(ValueError, match='duration_s'):
        TrackingEvaluator(TrackingCriteriaPolicy(), 30)
    assert evaluator().duration_s == 10


def test_initial_lock_transient_restarts_confirmation_and_can_pass():
    value = evaluator()
    sample(value, 0, snr=0, phase=False, locked=False)
    sample(value, .5)
    sample(value, .6, snr=0)
    healthy_until(value, 1, 10)
    result = value.finish(10)
    assert result.passed
    assert result.metrics['acquired_at'] == 2
    assert result.metrics['interruption_count'] == 0


def test_acquisition_confirmation_must_finish_within_timeout():
    value = evaluator()
    for at in (0, 1, 2):
        sample(value, at, snr=0, phase=False, locked=False)
    sample(value, 2.5)
    sample(value, 3)
    healthy_until(value, 3.5, 10)
    result = value.finish(10)
    assert result.verdict == 'FAIL'
    assert result.metrics['failure_reasons'] == ['acquisition_timeout']
    assert value.acquired_at is None


def test_acquisition_at_exact_timeout_is_allowed():
    value = evaluator()
    sample(value, 0, snr=0)
    sample(value, 1, snr=0)
    healthy_until(value, 2, 10)
    assert value.acquired_at == 3
    assert value.finish(10).passed


def test_missing_acquisition_evidence_produces_incomplete_not_performance_fail():
    value = evaluator()
    value.tick(3)
    healthy_until(value, 3, 10)
    result = value.finish(10)
    assert result.incomplete
    assert 'acquisition_timeout' not in result.metrics['failure_reasons']
    assert 'acquisition_evidence_incomplete' in result.metrics['quality_reasons']


def test_positive_snr_with_loss_of_lock_is_an_interruption():
    value = evaluator(acquisition=False)
    healthy_until(value, 0, 1)
    sample(value, 1.5, snr=12, locked=False)
    sample(value, 2, snr=12, phase=False)
    healthy_until(value, 2.5, 10)
    result = value.finish(10)
    assert result.passed
    assert result.metrics['interruption_count'] == 1
    assert result.metrics['interruption_total_s'] == 1


def test_short_recoveries_merge_and_confirmation_tail_is_excluded():
    value = evaluator(acquisition=False)
    sample(value, 0)
    sample(value, .5, snr=0)
    sample(value, 1)
    sample(value, 1.5, snr=0)
    healthy_until(value, 2, 10)
    result = value.finish(10)
    event, = result.metrics['interruptions']
    assert result.passed
    assert event['start_at'] == .5
    assert event['end_at'] == 2
    assert event['duration_s'] == 1.5
    assert result.metrics['interruption_count'] == 1


@pytest.mark.parametrize('bad_end,passed', [(2.5, True), (2.6, False)])
def test_single_event_limit_is_inclusive(bad_end, passed):
    value = evaluator(acquisition=False)
    sample(value, 0)
    sample(value, .5, snr=0)
    sample(value, 1, snr=0)
    sample(value, 2, snr=0)
    sample(value, bad_end)
    healthy_until(value, bad_end + .5, 10)
    result = value.finish(10)
    assert result.passed is passed
    assert ('interruption_duration_exceeded' in result.metrics['failure_reasons']) is (not passed)


@pytest.mark.parametrize('ratio,passed', [(.1, True), (.099, False)])
def test_cumulative_ratio_uses_planned_post_acquisition_window(ratio, passed):
    value = evaluator(rules=replace(policy(), interruption_ratio_max=ratio))
    healthy_until(value, 0, 2)
    sample(value, 2.5, snr=0)
    sample(value, 3.4)
    healthy_until(value, 3.9, 10)
    result = value.finish(10)
    assert result.metrics['observation_duration_s'] == 9
    assert result.metrics['interruption_total_s'] == pytest.approx(.9)
    assert result.passed is passed


@pytest.mark.parametrize('duration,expected', [(900, 3), (1800, 5), (1801, 6), (3600, 10)])
def test_event_quota_scales_by_recipe_duration(duration, expected):
    value = evaluator(duration=duration, acquisition=False)
    assert value.interruption_limit == expected


def test_separate_recovered_events_exceed_count_quota():
    value = evaluator(acquisition=False)
    healthy_until(value, 0, 1)
    sample(value, 1.5, snr=0)
    healthy_until(value, 2, 3)
    sample(value, 3.5, snr=0)
    healthy_until(value, 4, 10)
    result = value.finish(10)
    assert result.verdict == 'FAIL'
    assert result.metrics['interruption_count'] == 2
    assert result.metrics['interruption_limit'] == 1
    assert 'interruption_count_exceeded' in result.metrics['failure_reasons']


def test_tail_requires_confirmed_recovery():
    value = evaluator(acquisition=False)
    healthy_until(value, 0, 9)
    sample(value, 9.5, snr=0)
    sample(value, 10)
    result = value.finish(10)
    assert result.incomplete
    assert result.data_quality == 'COMPLETE'
    assert result.metrics['recovery_pending']


def test_failure_is_retained_alongside_later_quality_gap():
    value = evaluator(acquisition=False)
    sample(value, 0, snr=0)
    sample(value, 1, snr=0)
    sample(value, 2, snr=0)
    sample(value, 2.5, snr=0)
    healthy_until(value, 6, 10)
    result = value.finish(10)
    assert result.verdict == 'FAIL'
    assert result.performance_verdict == 'FAIL'
    assert result.data_quality == 'INCOMPLETE'
    assert 'tracking_sample_gap' in result.metrics['quality_reasons']


def test_event_crossing_unknown_gap_cannot_infer_long_interruption():
    value = evaluator(acquisition=False)
    sample(value, 0)
    sample(value, .5, snr=0)
    healthy_until(value, 4, 10)
    result = value.finish(10)
    assert result.incomplete
    assert result.metrics['failure_reasons'] == []
    event, = result.metrics['interruptions']
    assert event['duration_s'] is None
    assert event['evidence_complete'] is False
    assert event['known_duration_s'] == 0


def test_new_known_segment_after_gap_can_independently_prove_failure():
    value = evaluator(acquisition=False)
    sample(value, 0, snr=0)
    for at in (4, 5, 6, 7):
        sample(value, at, snr=0)
    healthy_until(value, 7.5, 10)
    result = value.finish(10)
    assert result.verdict == 'FAIL'
    assert result.data_quality == 'INCOMPLETE'
    assert 'interruption_duration_exceeded' in result.metrics['failure_reasons']


@pytest.mark.parametrize('snr,valid', [(math.nan, True), (math.inf, True), (-math.inf, True), (12, False)])
def test_invalid_fields_are_unknown_evidence(snr, valid):
    value = evaluator(acquisition=False)
    sample(value, 0)
    sample(value, .5, snr=snr, valid=valid)
    healthy_until(value, 1, 10)
    result = value.finish(10)
    assert result.incomplete
    assert result.metrics['interruption_count'] == 0
    assert 'invalid_tracking_sample' in result.metrics['quality_reasons']


def test_exact_gap_boundary_is_allowed_and_larger_gap_is_recorded():
    value = evaluator(acquisition=False)
    sample(value, 0)
    sample(value, 2)
    assert not value.quality_reasons
    sample(value, 4.001)
    assert 'tracking_sample_gap' in value.quality_reasons


def test_gap_breaks_initial_stability_confirmation():
    rules = replace(policy(), acquisition_stable_s=3, acquisition_timeout_s=6)
    value = evaluator(rules=rules, duration=12)
    sample(value, 0)
    sample(value, 1)
    sample(value, 3.5)
    sample(value, 4)
    assert value.acquired_at is None
    healthy_until(value, 4.5, 12)
    assert value.finish(12).incomplete


def test_missing_tail_is_a_quality_issue():
    value = evaluator(acquisition=False)
    healthy_until(value, 0, 7)
    result = value.finish(10)
    assert result.incomplete
    assert 'observation_end_evidence_missing' in result.metrics['quality_reasons']


def test_devices_have_independent_state_and_finish_is_immutable():
    first, second = evaluator(acquisition=False), evaluator(acquisition=False)
    healthy_until(first, 0, 10)
    for at in range(11):
        sample(second, at, snr=0)
    first_result = first.finish(10)
    assert first_result.passed
    assert second.finish(10).verdict == 'FAIL'
    sample(first, 11, snr=0)
    assert first.finish(11) is first_result


def test_out_of_window_and_repeated_samples_preserve_result():
    value = evaluator(acquisition=False)
    healthy_until(value, 0, 10)
    sample(value, 9, snr=0)
    sample(value, 10, snr=0)
    sample(value, 11, snr=0)
    assert value.finish(11).passed


def test_current_healthy_duration_resets_after_acquisition_and_tick_does_not_invent_samples():
    value = evaluator()
    healthy_until(value, 0, 1)
    assert value.acquired
    assert value.healthy_duration_s == 1
    value.tick(1.5)
    assert value.healthy_duration_s == 1
    sample(value, 2, snr=0)
    assert value.healthy_duration_s == 0
    healthy_until(value, 2.5, 3.5)
    assert value.healthy_duration_s == 1
    value.tick(5.6)
    assert value.healthy_duration_s == 0


def test_power_on_anchor_drives_elapsed_metrics():
    """耗时指标以上电锚点起算：锁定/GNSS/modem 各自独立记录。"""

    policy = replace(
        TrackingCriteriaPolicy(), acquisition_timeout_s=30.0, recovery_stable_s=3.0,
    )
    evaluator = TrackingEvaluator(policy, 60.0)
    evaluator.start(10.0, power_on_monotonic_s=4.0)
    assert evaluator.power_on_monotonic_s == 4.0

    # 帧距不超过最大采样间隔；连续健康 5 秒确认锁定。
    for at in (12.0, 14.0, 16.0, 18.0):
        evaluator.observe(at, True, True, 10.0, True,
                          gnss_fixed=True, modem_online=True)
    assert evaluator.acquired_at == pytest.approx(18.0)
    metrics = evaluator.metrics()
    assert metrics['lock_elapsed_s'] == pytest.approx(14.0)
    assert metrics['gnss_lock_elapsed_s'] == pytest.approx(8.0)
    assert metrics['modem_online_elapsed_s'] == pytest.approx(8.0)

    # GNSS 与 modem 未观察时保持未记录事实，不推断为零。
    pending = TrackingEvaluator(policy, 60.0)
    pending.start(10.0, power_on_monotonic_s=4.0)
    pending.observe(12.0, True, True, 10.0, True)
    pending_metrics = pending.metrics()
    assert pending_metrics['gnss_lock_elapsed_s'] is None
    assert pending_metrics['modem_online_elapsed_s'] is None
