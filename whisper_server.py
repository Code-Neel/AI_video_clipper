from flask import Flask, request, jsonify
from faster_whisper import WhisperModel
import subprocess
import tempfile
import pytesseract
import os
import cv2
import math
import re

app = Flask(__name__)

print("Loading Whisper model...")
model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)
print("Whisper model loaded!")

CHUNK_SECONDS = 300  # 5 minutes
YUNET_MODEL = "/app/models/face_detection_yunet_2023mar.onnx"


def detect_visible_text(frame):
    """
    Detect visible text using Tesseract OCR.
    Returns text bounding boxes in source-frame coordinates.
    """
    try:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        data = pytesseract.image_to_data(
            rgb_frame,
            output_type=pytesseract.Output.DICT,
            config="--psm 11"
        )

        text_regions = []

        for i, word in enumerate(data["text"]):
            word = word.strip()

            try:
                confidence = float(data["conf"][i])
            except (ValueError, TypeError):
                continue

            # Lower the threshold so partially recognized stylized titles
            # can still trigger the crop-safety check.
            if word and confidence >= 20:
                x = data["left"][i]
                y = data["top"][i]
                w = data["width"][i]
                h = data["height"][i]

                text_regions.append({
                    "text": word,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "confidence": confidence
                })

        return text_regions

    except Exception as exc:
        print(f"OCR detection warning: {exc}", flush=True)
        return None


_face_detector = None
_face_detector_size = None


def detect_faces(frame, confidence=0.5):
    """Return detected face boxes as (x, y, width, height)."""
    global _face_detector, _face_detector_size

    height, width = frame.shape[:2]
    frame_size = (width, height)

    # Reuse the detector rather than constructing a new YuNet instance
    # for every sampled frame.
    if _face_detector is None:
        _face_detector = cv2.FaceDetectorYN.create(
            YUNET_MODEL,
            "",
            frame_size,
            confidence,
            0.3,
            5000
        )
        _face_detector_size = frame_size
    elif _face_detector_size != frame_size:
        _face_detector.setInputSize(frame_size)
        _face_detector_size = frame_size

    _, faces = _face_detector.detect(frame)

    if faces is None:
        return []

    return [
        tuple(int(round(value)) for value in face[:4])
        for face in faces
    ]


def smooth_positions(positions, alpha=0.2):
    """Smooth crop-centre positions to reduce sudden camera movement."""
    if not positions:
        return []

    smoothed = [float(positions[0])]

    for current in positions[1:]:
        previous = smoothed[-1]
        smoothed.append(
            alpha * float(current) + (1 - alpha) * previous
        )

    return smoothed


def choose_face_center(faces, frame_width, previous_center=None):
    """Choose a face centre, preferring the previous position for continuity."""
    if not faces:
        return previous_center if previous_center is not None else frame_width / 2

    centers = [
        x + width / 2
        for x, y, width, height in faces
    ]

    if previous_center is None:
        # Start with the largest detected face.
        largest = max(faces, key=lambda face: face[2] * face[3])
        return largest[0] + largest[2] / 2

    # Prefer the face closest to the previous crop centre.
    return min(centers, key=lambda center: abs(center - previous_center))


def analyse_clip_framing(file_path, start_time, end_time, sample_fps=5):
    """Detect faces within a clip interval and return smoothed crop centres."""
    cap = cv2.VideoCapture(file_path)

    if not cap.isOpened():
        raise ValueError(f"Could not open video: {file_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    duration = max(0, end_time - start_time)

    if duration <= 0:
        cap.release()
        raise ValueError("end_time must be greater than start_time")

    sample_step = max(1, int(round(fps / sample_fps)))
    frame_index = int(start_time * fps)
    last_index = int(end_time * fps)

    times = []
    centers = []
    face_detections = 0
    sampled_frames = 0
    previous_center = None

    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)

        while frame_index < last_index:
            ok, frame = cap.read()
            if not ok:
                break

            faces = detect_faces(frame)
            sampled_frames += 1

            if faces:
                face_detections += 1

            times.append(frame_index / fps - start_time)

            if faces:
                center = choose_face_center(
                    faces,
                    frame_width,
                    previous_center
                )
                centers.append(center)
                previous_center = center

            frame_index += sample_step
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)

    finally:
        cap.release()

    face_ratio = (
        face_detections / sampled_frames
        if sampled_frames > 0 else 0
    )

    return {
        "duration": duration,
        "frame_width": frame_width,
        "times": times,
        "centers": smooth_positions(centers, alpha=0.2),
        "sampled_frames": sampled_frames,
        "face_ratio": face_ratio
    }


def get_duration(file_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ],
        capture_output=True,
        text=True,
        check=True
    )

    return float(result.stdout.strip())


@app.route("/transcribe", methods=["POST"])
def transcribe():
    data = request.get_json()
    file_path = data["file_path"]

    print(f"Transcribing: {file_path}")

    total_duration = get_duration(file_path)

    transcript = []
    detected_language = None
    start = 0.0

    while start < total_duration:
        chunk_duration = min(
            CHUNK_SECONDS,
            total_duration - start
        )

        print(
            f"Transcribing chunk "
            f"{start:.1f}s - {start + chunk_duration:.1f}s"
        )

        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False
        ) as temp:
            chunk_file = temp.name

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss", str(start),
                    "-i", file_path,
                    "-t", str(chunk_duration),
                    "-vn",
                    "-ac", "1",
                    "-ar", "16000",
                    "-c:a", "pcm_s16le",
                    chunk_file
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True
            )

            # Let Whisper detect language for each chunk. Do not lock all later
            # chunks to the first chunk's language, since a video may switch between
            # English, Hindi, and Hinglish over time.
            segments, info = model.transcribe(
                chunk_file,
                beam_size=5,
                word_timestamps=True
            )

            if detected_language is None:
                detected_language = info.language

            for segment in segments:
                words = []

                if segment.words:
                    for word in segment.words:
                        words.append({
                            "start": round(word.start + start, 3),
                            "end": round(word.end + start, 3),
                            "word":(word.word.strip())
                        })

                transcript.append({
                    "start": round(segment.start + start, 3),
                    "end": round(segment.end + start, 3),
                    "text":(segment.text.strip()),
                    "words": words
                })

        finally:
            if os.path.exists(chunk_file):
                os.remove(chunk_file)

        start += chunk_duration

    print("Transcription complete!")

    return jsonify({
        "language": detected_language,
        "duration": total_duration,
        "segments": transcript
    })


@app.route("/frame", methods=["POST"])
def frame_clip():
    data = request.get_json(force=True)

    file_path = data["file_path"]
    start_time = float(data["start_time"])
    end_time = float(data["end_time"])
    output_path = data["output_path"]

    framing_mode = data.get("framing_mode", "smart_crop")
    requested_framing_mode = framing_mode

    if framing_mode not in ("smart_crop", "preserve_scene", "auto"):
        return jsonify({"error": "Invalid framing_mode"}), 400

    if not os.path.isfile(file_path):
        return jsonify({"error": "Source video not found"}), 404

    if start_time < 0 or end_time <= start_time:
        return jsonify({"error": "Invalid clip timestamps"}), 400

    auto_analysis = None
    text_regions = []
    ocr_failed = False

    if framing_mode == "auto":
        auto_analysis = analyse_clip_framing(
            file_path, start_time, end_time
        )

        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            cap.release()
            return jsonify({"error": "Could not open source video"}), 400

        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        check_times = [
            start_time,
            (start_time + end_time) / 2,
            max(start_time, end_time - 0.5)
        ]

        try:
            for check_time in check_times:
                cap.set(cv2.CAP_PROP_POS_MSEC, check_time * 1000)
                success, frame = cap.read()

                if success:
                    detected = detect_visible_text(frame)
                    if detected is None:
                        ocr_failed = True
                    else:
                        text_regions.extend(detected)
        finally:
            cap.release()

        face_ratio = auto_analysis.get("face_ratio", 0.0)
        sampled_frames = auto_analysis.get("sampled_frames", 0)
        centers = auto_analysis.get("centers", [])

        # Conservative fallback if analysis produced no usable samples.
        if sampled_frames == 0 or source_width <= 0 or source_height <= 0:
            framing_mode = "preserve_scene"
        elif source_width / source_height < 1.2:
            # Portrait or near-square source: avoid an unnecessary crop.
            framing_mode = "preserve_scene"
        elif face_ratio < 0.25 or not centers:
            framing_mode = "preserve_scene"
        else:
            framing_mode = "smart_crop"

    try:
        if auto_analysis is not None:
            framing = auto_analysis
        else:
            framing = analyse_clip_framing(
                file_path, start_time, end_time
            )

        frame_width = framing["frame_width"]
        centers = framing["centers"]

        if centers:
            # A median is less sensitive than a mean to occasional
            # extreme detections. This remains a single fixed crop per clip.
            sorted_centers = sorted(centers)
            middle = len(sorted_centers) // 2
            if len(sorted_centers) % 2:
                crop_center = sorted_centers[middle]
            else:
                crop_center = (
                    sorted_centers[middle - 1] + sorted_centers[middle]
                ) / 2
        else:
            crop_center = frame_width / 2

        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            cap.release()
            return jsonify({"error": "Could not open source video"}), 400

        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        crop_width = int(min(frame_width, frame_height * 9 / 16))
        crop_width = max(2, crop_width - crop_width % 2)
        crop_x = int(crop_center - crop_width / 2)
        crop_x = max(0, min(crop_x, frame_width - crop_width))

        # Only Auto mode uses OCR to override the face-based decision.
        # If OCR detects a text region outside the proposed crop, preserve
        # the full scene. OCR may miss stylized text, so this is a safeguard,
        # not a guarantee that all important content will be detected.
        text_cut_off = False

        if requested_framing_mode == "auto":
            for region in text_regions:
                text_left = region["x"]
                text_right = text_left + region["w"]

                if text_left < crop_x or text_right > crop_x + crop_width:
                    text_cut_off = True
                    break

            if text_cut_off or ocr_failed:
                framing_mode = "preserve_scene"

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        if framing_mode == "preserve_scene":
            filter_graph = (
                "[0:v]split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,gblur=sigma=30[background];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease"
                "[foreground];"
                "[background][foreground]"
                "overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
            )

            command = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", file_path,
                "-t", str(end_time - start_time),
                "-filter_complex", filter_graph,
                "-map", "[v]",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                output_path
            ]
        else:
            command = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", file_path,
                "-t", str(end_time - start_time),
                "-vf",
                f"crop={crop_width}:{int(crop_width * 16 / 9)}:{crop_x}:0,"
                "scale=1080:1920:flags=lanczos",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                output_path
            ]

        subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True
        )

        return jsonify({
            "status": "ok",
            "output_path": output_path,
            "duration": round(end_time - start_time, 3),
            "crop_center": round(crop_center, 2),
            "framing_mode": framing_mode,
            "sampled_frames": framing.get("sampled_frames", 0),
            "face_ratio": round(framing.get("face_ratio", 0), 3)
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


app.run(
    host="0.0.0.0",
    port=5000
)
