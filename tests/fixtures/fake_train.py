from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


root = Path.cwd()
(root / "outputs").mkdir(parents=True, exist_ok=True)
(root / "logs").mkdir(parents=True, exist_ok=True)
(root / "predictions").mkdir(parents=True, exist_ok=True)

(root / "outputs" / "metrics.json").write_text(
    json.dumps(
        [
            {"metric_name": "accuracy", "value": 0.875, "step": 1, "metadata": {"split": "validation"}},
            {"metric_name": "loss", "value": 0.125, "step": 1, "metadata": {"split": "validation"}},
        ]
    ),
    encoding="utf-8",
)
(root / "logs" / "train.log").write_text("epoch=1 accuracy=0.875\n", encoding="utf-8")
(root / "predictions" / "prediction-001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
with (root / "outputs" / "confusion_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerows([["actual", "predicted", "count"], ["cat", "cat", 8], ["dog", "cat", 1]])

print("fake training stdout")
print("fake training stderr", file=sys.stderr)
