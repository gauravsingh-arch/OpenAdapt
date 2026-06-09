import cv2

def on_mouse(event, x, y, flags, param):
    if event == cv2.EVENT_MOUSEMOVE:
        pixel = img[y, x]
        print(f"X: {x}, Y: {y} → BGR: {pixel}")

img = cv2.imread("Current_Screenshot.png")
cv2.imshow("Image", img)
cv2.setMouseCallback("Image", on_mouse)
cv2.waitKey(0)