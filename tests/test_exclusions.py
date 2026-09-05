from datetime import date

from school_pipeline.models import EventType, SchoolEvent, SourceRef
from school_pipeline.pipeline.exclusions import ExclusionRule, ExclusionRules


def _event(**overrides) -> SchoolEvent:
    defaults = dict(
        type=EventType.EVENT,
        title="身だしなみチェック",
        date=date(2026, 9, 1),
        subject=None,
        confidence=0.9,
        source=SourceRef(source_type="pdf", source_id="p1", label="規律検査実施について.pdf"),
    )
    defaults.update(overrides)
    return SchoolEvent(**defaults)


def test_no_rules_keeps_everything():
    events = [_event(), _event(title="別の予定")]
    kept, excluded = ExclusionRules().split(events)
    assert kept == events
    assert excluded == []


def test_title_contains_matches_partially():
    rules = ExclusionRules([ExclusionRule(title_contains="身だしなみ")])
    kept, excluded = rules.split([_event(), _event(title="定期試験")])
    assert len(kept) == 1
    assert kept[0].title == "定期試験"
    assert excluded[0][0].title == "身だしなみチェック"


def test_title_match_ignores_width_and_case():
    """全角で書いたルールが半角・小文字のタイトルにも当たること。"""
    rules = ExclusionRules([ExclusionRule(title_contains="ＫＰＴ")])
    kept, excluded = rules.split([_event(title="kpt 11A")])
    assert kept == []
    assert len(excluded) == 1


def test_conditions_combine_with_and():
    """条件を足すほど対象が狭くなること(広く消しすぎる事故を防ぐ)。"""
    rule = ExclusionRule(title_contains="チェック", type=EventType.QUIZ)
    kept, excluded = ExclusionRules([rule]).split([_event()])  # type が EVENT なので外れない
    assert len(kept) == 1
    assert excluded == []


def test_identity_matches_either_bare_key_or_qualified_identity():
    event = _event(subject="英語", identity_key="B-1")
    assert ExclusionRule(identity="B-1").matches(event)
    assert ExclusionRule(identity="英語:B-1").matches(event)
    assert not ExclusionRule(identity="B-2").matches(event)


def test_identity_rule_does_not_match_events_without_a_key():
    assert not ExclusionRule(identity="B-1").matches(_event(identity_key=None))


def test_subject_must_match_exactly():
    rule = ExclusionRule(subject="英語")
    assert rule.matches(_event(subject="英語"))
    assert not rule.matches(_event(subject="英語表現"))


def test_source_label_contains():
    rule = ExclusionRule(source_label_contains="規律検査")
    assert rule.matches(_event())
    assert not rule.matches(
        _event(source=SourceRef(source_type="pdf", source_id="p2", label="週末課題.pdf"))
    )


def test_rule_requires_at_least_one_condition():
    try:
        ExclusionRule(reason="理由だけ")
    except ValueError:
        return
    raise AssertionError("条件のないルールは弾かれるべき")


def test_load_missing_file_yields_no_rules(tmp_path):
    assert ExclusionRules.load(tmp_path / "none.yaml").rules == []
    assert ExclusionRules.load(None).rules == []


def test_load_reads_rules_from_yaml(tmp_path):
    path = tmp_path / "exclusions.yaml"
    path.write_text(
        "exclusions:\n"
        "  - title_contains: 身だしなみ\n"
        "    type: school_event\n"
        "    reason: 本人に該当しないため\n",
        encoding="utf-8",
    )
    rules = ExclusionRules.load(path)
    assert len(rules.rules) == 1
    assert rules.rules[0].type == EventType.EVENT
    assert rules.rules[0].reason == "本人に該当しないため"

    kept, excluded = rules.split([_event()])
    assert kept == []
    assert excluded[0][1].reason == "本人に該当しないため"


def test_a_broken_rule_does_not_disable_the_others(tmp_path):
    """1つ壊れていても残りは効くこと。

    黙って全ルールが無効になると、消したはずの予定が復活して原因が分かりにくい。
    """
    path = tmp_path / "exclusions.yaml"
    path.write_text(
        "exclusions:\n"
        "  - reason: 条件がないので不正\n"
        "  - title_contains: 身だしなみ\n",
        encoding="utf-8",
    )
    rules = ExclusionRules.load(path)
    assert len(rules.rules) == 1
    assert rules.split([_event()])[0] == []


def test_empty_exclusions_key_is_tolerated(tmp_path):
    path = tmp_path / "exclusions.yaml"
    path.write_text("exclusions:\n", encoding="utf-8")
    assert ExclusionRules.load(path).rules == []
