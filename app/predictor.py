"""
Swappable predictor for the Voyager3 dashboard.

Replace the example below with your own model. The dashboard only needs:

    Predictor(config)          -> loads your model (config comes from config.json)
    .classes                   -> list of class names
    .healthy_label             -> the class name that means "no fault"
    .ok / .error               -> set ok=False and error="..." if loading fails
    .predict(wave)             -> returns {"label", "confidence", "probs"}

`wave` is a dict of the latest segment in g: {"x": [...], "y": [...], "z": [...]}.

The example is a simple RMS threshold so the dashboard works out of the box
with no trained model. Swap it for a PyTorch, scikit-learn or any other model.
"""

import numpy as np


class Predictor:
    def __init__(self, config):
        self.ok = False
        self.error = None
        self.classes = ["Normal", "Faulty"]
        self.healthy_label = config.get("healthy_label", "Normal")
        self.model_dir = config.get("model_dir")   # point this to your model files

        try:
            # ---- Load your model here ----
            # Example (PyTorch):
            #   import torch
            #   self.model = torch.load(os.path.join(self.model_dir, "model.pt"))
            #   self.model.eval()
            self.rms_threshold = float(config.get("rms_threshold_g", 0.5))
            self.ok = True
        except Exception as e:
            self.error = str(e)

    def predict(self, wave):
        # ---- Replace with your model's inference ----
        rms = float(np.sqrt(np.mean(np.concatenate(
            [np.asarray(wave[ax], dtype=np.float32) for ax in ("x", "y", "z")]) ** 2)))

        p_fault = float(np.clip(rms / (2 * self.rms_threshold), 0.0, 1.0))
        probs = {"Normal": 1.0 - p_fault, "Faulty": p_fault}
        label = max(probs, key=probs.get)

        return {
            "label": label,
            "confidence": probs[label],
            "probs": {k: round(v, 4) for k, v in probs.items()},
        }
