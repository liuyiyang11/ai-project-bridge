import pytest

from bridge.task_parser import TaskParseError, parse_rework_comment, parse_task_body


def test_parse_valid_code_task():
    task = parse_task_body(
        """<!-- AI_BRIDGE_TASK -->

```yaml
version: 1
task_type: code
project: demo
title: Improve decoder
goal: Make the decoder safer.
instructions: Keep the API unchanged.
acceptance:
  - tests pass
```
"""
    )

    assert task.task_type == "code"
    assert task.project == "demo"
    assert task.acceptance == ["tests pass"]


def test_task_without_marker_is_rejected():
    with pytest.raises(TaskParseError, match="marker"):
        parse_task_body("```yaml\nversion: 1\ntask_type: code\n```")


def test_unknown_task_type_is_rejected():
    with pytest.raises(TaskParseError, match="task_type"):
        parse_task_body(
            """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: shell
project: demo
title: Bad
```
"""
        )


def test_arbitrary_command_field_is_rejected():
    with pytest.raises(TaskParseError, match="command"):
        parse_task_body(
            """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Bad
command: powershell Remove-Item *
```
"""
        )


@pytest.mark.parametrize("bad_path", ["../brief.md", "C:/secret/brief.md", "/etc/passwd"])
def test_presentation_paths_must_be_project_relative(bad_path):
    with pytest.raises(TaskParseError, match="relative"):
        parse_task_body(
            f"""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: presentation
project: demo
title: Slides
brief: {bad_path}
```
"""
        )


def test_parse_rework_comment_requires_only_instruction():
    instruction = parse_rework_comment(
        """<!-- AI_BRIDGE_REWORK -->
```yaml
instruction: |
  Fix the decoder shape mismatch.
  Add a regression test.
```
"""
    )
    assert "regression test" in instruction


def test_rework_comment_without_marker_is_rejected():
    with pytest.raises(TaskParseError, match="REWORK"):
        parse_rework_comment("please fix it")
