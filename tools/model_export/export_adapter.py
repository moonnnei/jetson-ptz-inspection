"""Leave YOLO26's one-to-one network intact; move TopK/Gather to the host."""
from types import MethodType

def raw_predictions(self, predictions):
    # Predictions: [batch, anchors, 84], decoded xyxy + 80 class probabilities.
    return predictions

def use_host_topk(model):
    head = model.model.model[-1]
    assert head.end2end and head.nc == 80
    head.postprocess = MethodType(raw_predictions, head)
