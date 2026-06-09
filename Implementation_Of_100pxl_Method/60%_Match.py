import cv2
import numpy as np
from PIL import Image


def find_matching_crop(i1_path: str, i2_path: str, threshold: float = 0.70,
                       scale_range: tuple = (0.5, 2.0), scale_steps: int = 30) -> Image.Image | None:
    i1 = cv2.imread(i1_path)
    i2 = cv2.imread(i2_path)

    if i1 is None or i2 is None:
        raise FileNotFoundError("Could not load one or both images.")

    h2, w2 = i2.shape[:2]
    h1, w1 = i1.shape[:2]

    best_score = -1
    best_loc = None
    best_scale = None

    for scale in np.linspace(scale_range[0], scale_range[1], scale_steps):
        new_w = int(w1 * scale)
        new_h = int(h1 * scale)

        # Skip if scaled template is larger than I2
        if new_w > w2 or new_h > h2 or new_w < 1 or new_h < 1:
            continue

        template = cv2.resize(i1, (new_w, new_h), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(i2, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_score:
            best_score = max_val
            best_loc = max_loc
            best_scale = scale

    if best_score < threshold:
        print(f"Best match score {best_score:.2%} is below the {threshold:.0%} threshold. No match found.")
        return None

    x, y = best_loc
    crop_w = int(w1 * best_scale)
    crop_h = int(h1 * best_scale)
    cropped = i2[y:y + crop_h, x:x + crop_w]

    print(f"Match found at ({x}, {y}), scale {best_scale:.2f}x, score {best_score:.2%}")
    return Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))


if __name__ == "__main__":


    result = find_matching_crop("cropped_696_432.png", "Current_Screenshot.png")
    if result:
        result.save("matched_crop.png")
        print("Saved cropped match to matched_crop.png")
