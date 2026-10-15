"""Crop real evidence with installed OpenCV. Compatible with Python 2.7/3."""
from __future__ import print_function
import json
import math
import sys


def main():
    import cv2
    if sys.argv[1:] == ['--check']:
        print(json.dumps(dict(opencv=cv2.__version__)))
        return
    if len(sys.argv) != 4:
        raise ValueError('Expected image, crop destination, normalized bbox')
    box = json.loads(sys.argv[3])
    if len(box) != 4 or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in box):
        raise ValueError('Invalid bbox')
    image = cv2.imread(sys.argv[1])
    if image is None:
        raise ValueError('Invalid image')
    h, w = image.shape[:2]
    x1, y1, x2, y2 = [int(v * size) for v, size in zip(box, (w, h, w, h))]
    crop = image[y1:y2, x1:x2]
    if not crop.size:
        raise ValueError('Empty crop')
    sharpness = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
    if math.isnan(sharpness) or math.isinf(sharpness) or not cv2.imwrite(sys.argv[2], crop):
        raise ValueError('Cannot save crop')
    print(json.dumps(dict(sharpness=round(sharpness, 2), crop_width=crop.shape[1], crop_height=crop.shape[0])))


if __name__ == '__main__':
    main()
