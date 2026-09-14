import pytest

from cua.schema import (
    ActionType,
    CapabilityArtifact,
    CapabilityMeta,
    Checkpoint,
    CheckpointKind,
    InputParam,
    Locator,
    LocatorStrategy,
    OutputField,
    ParamType,
    Selector,
    Step,
    TargetBinding,
    render_template,
    template_param_names,
)


def _min_artifact() -> CapabilityArtifact:
    return CapabilityArtifact(
        capability=CapabilityMeta(id="app.cap", name="Cap", description="d"),
        target=TargetBinding(app_id="app", base_url="http://h:1/"),
        inputs=[InputParam(name="member_id", description="id"),
                InputParam(name="passcode", secret=True)],
        outputs=[OutputField(name="bal", type=ParamType.STRING)],
        steps=[Step(id="s1", action=ActionType.FILL, value="{{member_id}}",
                    target=Locator(description="f",
                                   primary=Selector(strategy=LocatorStrategy.NAME_ATTR, value="member_id")))],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="ok"),
    )


def test_round_trip_serialization():
    a = _min_artifact()
    text = a.to_json()
    b = CapabilityArtifact.from_json(text)
    assert b.capability.id == "app.cap"
    assert b.steps[0].value == "{{member_id}}"
    assert b.secret_input_names() == {"passcode"}


def test_validate_params_missing_and_unknown():
    a = _min_artifact()
    with pytest.raises(ValueError):
        a.validate_params({})  # missing member_id + passcode
    with pytest.raises(ValueError):
        a.validate_params({"member_id": "1", "passcode": "x", "extra": "y"})
    a.validate_params({"member_id": "1", "passcode": "x"})  # ok


def test_role_selector_requires_role():
    with pytest.raises(ValueError):
        Selector(strategy=LocatorStrategy.ROLE, value="Search")  # role missing
    Selector(strategy=LocatorStrategy.ROLE, role="button", value="Search")  # ok


def test_step_shape_validation():
    with pytest.raises(ValueError):
        Step(id="g", action=ActionType.GOTO)  # GOTO requires url
    with pytest.raises(ValueError):
        Step(id="c", action=ActionType.CLICK)  # CLICK requires target


def test_templating():
    assert render_template("id={{member_id}}", {"member_id": "42"}) == "id=42"
    assert template_param_names("{{a}}/{{b}}") == {"a", "b"}
    with pytest.raises(KeyError):
        render_template("{{missing}}", {})


def test_default_policy_action_types():
    a = _min_artifact()
    assert ActionType.CLICK in a.policy.allowed_action_types
