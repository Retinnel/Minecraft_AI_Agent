import cv2


def preprocess_frame(frame, width=1280, height=720, quality=80):
    if frame is None:
        return None
    try:
        resized = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return enc.tobytes()
    except Exception:
        return None
