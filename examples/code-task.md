<!-- AI_BRIDGE_TASK -->

```yaml
version: 1
task_type: code
project: unetmamba
title: Improve decoder skip fusion
goal: |
  Implement the already-reviewed decoder skip-fusion change.
instructions: |
  Preserve the existing input/output interface.
  Do not modify the backbone.
  Preserve checkpoint compatibility.
acceptance:
  - existing API unchanged
  - configured quick_test passes
  - no unrelated changes
```

