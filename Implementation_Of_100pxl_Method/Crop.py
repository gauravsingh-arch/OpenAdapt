from PIL import Image


def crop_around_pixel(image_path: str, x: int, y: int) -> Image.Image:
    img = Image.open(image_path)
    left = x - 75
    upper = y - 75
    right = x + 75
    lower = y + 75
    return img.crop((left, upper, right, lower))


if __name__ == "__main__":
    cropped = crop_around_pixel("capture_1_step_1.png", 696, 432)
    output_path = "cropped_696_432_original.png"
    cropped.save(output_path)
    
    cropped = cropped.resize((400, 400), Image.LANCZOS)
    output_path = "cropped_696_432.png"
    cropped.save(output_path)
    print(f"Saved cropped image to {output_path}")
