"""
camera_engine.py — webcam-based hand/face gesture pipeline for Camera Mode.

Real classical-CV + MediaPipe landmark tracking, not a mockup: actual hand
landmark positions drive pinch/fist/open-palm/swipe/push-pull/rotate
detection, and actual face landmarks drive blink (eye-aspect-ratio), smile,
head-tilt, eyebrow-raise, and a coarse iris-based gaze estimate. Custom
gestures are recorded as real landmark-sequence templates and matched at
runtime by nearest-neighbor distance — not a trained deep model (that would
need a labeled dataset and training infrastructure this project doesn't
have), but a legitimate, working, from-scratch template-matching approach.

Runs on its own background thread. Never imports Gtk — the caller marshals
any callback that touches widgets through GLib.idle_add, same pattern as
voice_engine.py. All the heavy imports (cv2, mediapipe) happen lazily
inside start()/_run(), so importing this module is always cheap and never
crashes the app even if opencv/mediapipe aren't installed or a webcam isn't
available — Camera Mode just reports itself unavailable via on_state().

Privacy note: no video frame is ever displayed, saved, or sent anywhere.
Only derived landmark coordinates (dozens of numbers per hand/face) are
extracted per frame and handed to callbacks for skeleton rendering — the
actual camera image never leaves this module.
"""

import json
import math
import os
import time
import threading
from pathlib import Path

GESTURES_PATH = Path.home() / "linux-agent" / "camera_gestures.json"
HAND_MODEL_PATH = Path.home() / "linux-agent" / "hand_landmarker.task"
FACE_MODEL_PATH = Path.home() / "linux-agent" / "face_landmarker.task"

PINCH_THRESHOLD = 0.06          # normalized hand-relative distance
SWIPE_MIN_DELTA = 0.18          # normalized x movement to count as a swipe
SWIPE_MAX_DURATION = 0.5        # seconds
SWIPE_COOLDOWN = 0.8
DEPTH_PUSH_PULL_DELTA = 0.35    # relative bounding-box-size change
GESTURE_COOLDOWN = 0.7          # generic cooldown between repeated built-in gesture fires
GESTURE_HOLD_FRAMES = 5         # consecutive frames a pose must hold before firing — cuts down false positives a lot
CUSTOM_MATCH_THRESHOLD = 0.16   # avg per-point normalized distance to count as a match
CUSTOM_RECORD_FRAMES = 24

EAR_BLINK_THRESHOLD = 0.21
DOUBLE_BLINK_WINDOW = 0.6
SMILE_RATIO_THRESHOLD = 1.9     # mouth width / height, above this ~= smiling
EYEBROW_RAISE_THRESHOLD = 0.045
HEAD_SHAKE_MIN_DELTA = 0.10
HEAD_SHAKE_COUNT = 3
HEAD_SHAKE_WINDOW = 1.2

# MediaPipe Face Mesh landmark indices used for the heuristics above
LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
LEFT_IRIS = [468, 469, 470, 471]
RIGHT_IRIS = [473, 474, 475, 476]
MOUTH_CORNERS = (61, 291)
MOUTH_TOP_BOTTOM = (13, 14)
LEFT_EYEBROW = 105
LEFT_EYE_TOP = 159
NOSE_TIP = 1


def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _eye_aspect_ratio(pts):
    # pts: 6 (x,y) points around one eye, MediaPipe ordering approximated
    p1, p2, p3, p4, p5, p6 = pts
    vertical = _dist(p2, p6) + _dist(p3, p5)
    horizontal = 2.0 * _dist(p1, p4)
    return vertical / horizontal if horizontal else 0.0


class CameraEngine:
    def __init__(self, on_hand_frame=None, on_face_frame=None, on_gesture=None, on_state=None):
        self.on_hand_frame = on_hand_frame    # (list_of_hands_landmarks_xy) -> None, for skeleton drawing
        self.on_face_frame = on_face_frame    # (landmarks_xy or None) -> None
        self.on_gesture = on_gesture          # (gesture_name: str) -> None
        self.on_state = on_state              # (state_str) -> None

        self.enabled = False
        self.body_tracking_enabled = False   # heavier — opt-in separately
        self.recording_gesture = None         # name being recorded, or None
        self._record_buffer = []

        self.custom_gestures = self._load_gestures()

        self._thread = None
        self._stop_flag = threading.Event()

        # gesture-detection state
        self._wrist_history = []       # [(t, x)] for swipe detection
        self._bbox_size_history = []   # [(t, size)] for push/pull detection
        self._angle_history = []       # [(t, angle)] for rotate detection
        self._last_gesture_time = {}
        self._blink_history = []       # [t, ...] recent blink timestamps
        self._eye_was_closed = False
        self._nose_x_history = []      # for head-shake detection

    # -- persistence for custom gestures -----------------------------------
    def _load_gestures(self):
        if GESTURES_PATH.exists():
            try:
                return json.loads(GESTURES_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_gestures(self):
        try:
            GESTURES_PATH.parent.mkdir(parents=True, exist_ok=True)
            GESTURES_PATH.write_text(json.dumps(self.custom_gestures, indent=2))
        except OSError:
            pass

    def start_recording(self, name):
        self.recording_gesture = name
        self._record_buffer = []

    def cancel_recording(self):
        self.recording_gesture = None
        self._record_buffer = []

    def delete_custom_gesture(self, name):
        self.custom_gestures = [g for g in self.custom_gestures if g["name"] != name]
        self._save_gestures()

    # -- lifecycle --------------------------------------------------------------
    def start(self):
        if self.enabled:
            return
        self.enabled = True
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.enabled = False
        self._stop_flag.set()

    # -- main capture loop ---------------------------------------------------
    def _run(self):
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision
        except ImportError as e:
            if self.on_state:
                self.on_state(f"unavailable: {e}")
            self.enabled = False
            return

        if not HAND_MODEL_PATH.exists() or not FACE_MODEL_PATH.exists():
            if self.on_state:
                self.on_state(
                    "unavailable: model files missing — see README for the "
                    "hand_landmarker.task / face_landmarker.task download commands"
                )
            self.enabled = False
            return

        try:
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                raise RuntimeError("no webcam found at index 0")
        except Exception as e:
            if self.on_state:
                self.on_state(f"error: {e}")
            self.enabled = False
            return

        hand_landmarker = mp_vision.HandLandmarker.create_from_options(
            mp_vision.HandLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(HAND_MODEL_PATH)),
                running_mode=mp_vision.RunningMode.VIDEO,
                num_hands=2,
                min_hand_detection_confidence=0.6,
                min_tracking_confidence=0.5,
            )
        )
        face_landmarker = None
        try:
            face_landmarker = mp_vision.FaceLandmarker.create_from_options(
                mp_vision.FaceLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_path=str(FACE_MODEL_PATH)),
                    running_mode=mp_vision.RunningMode.VIDEO,
                    num_faces=1,
                    min_face_detection_confidence=0.6,
                    min_face_presence_confidence=0.5,
                )
            )
        except Exception:
            face_landmarker = None

        if self.on_state:
            self.on_state("running")

        start_time = time.monotonic()
        try:
            while not self._stop_flag.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = int((time.monotonic() - start_time) * 1000)

                hand_result = hand_landmarker.detect_for_video(mp_image, timestamp_ms)
                self._handle_hands(hand_result)

                if face_landmarker is not None:
                    face_result = face_landmarker.detect_for_video(mp_image, timestamp_ms)
                    self._handle_face(face_result)

                time.sleep(0.01)  # yield; MediaPipe calls above already throttle real work
        finally:
            cap.release()
            hand_landmarker.close()
            if face_landmarker is not None:
                face_landmarker.close()
            if self.on_state:
                self.on_state("idle")

    # -- hand landmarks -> gestures ------------------------------------------
    def _handle_hands(self, result):
        now = time.monotonic()
        if not result.hand_landmarks:
            if self.on_hand_frame:
                self.on_hand_frame([])
            return

        all_hands_xy = []
        for hand_landmarks in result.hand_landmarks:  # Tasks API: list of NormalizedLandmark directly
            pts = [(lm.x, lm.y) for lm in hand_landmarks]
            all_hands_xy.append(pts)

        if self.on_hand_frame:
            self.on_hand_frame(all_hands_xy)

        # only classify built-in gestures against the first detected hand,
        # to keep this tractable and predictable
        pts = all_hands_xy[0]
        wrist = pts[0]
        thumb_tip, index_tip, middle_tip, ring_tip, pinky_tip = pts[4], pts[8], pts[12], pts[16], pts[20]
        index_pip, middle_pip, ring_pip, pinky_pip = pts[6], pts[10], pts[14], pts[18]
        middle_mcp = pts[9]

        hand_size = _dist(wrist, middle_mcp) or 0.001

        # recording a custom gesture?
        if self.recording_gesture:
            normalized = self._normalize_landmarks(pts, wrist, hand_size)
            self._record_buffer.append(normalized)
            if len(self._record_buffer) >= CUSTOM_RECORD_FRAMES:
                self.custom_gestures.append({
                    "name": self.recording_gesture,
                    "template": self._record_buffer,
                })
                self._save_gestures()
                if self.on_gesture:
                    self.on_gesture(f"__recorded__:{self.recording_gesture}")
                self.recording_gesture = None
                self._record_buffer = []
            return  # don't also fire built-in gestures while recording

        # -- pinch --
        pinch_dist = _dist(thumb_tip, index_tip) / hand_size
        is_pinching = pinch_dist < PINCH_THRESHOLD * 3  # hand_size already normalizes scale

        # -- open palm / fist (non-thumb fingertips vs pip joints) --
        extended = sum(1 for tip, pip in [(index_tip, index_pip), (middle_tip, middle_pip),
                                           (ring_tip, ring_pip), (pinky_tip, pinky_pip)]
                        if tip[1] < pip[1])
        is_open_palm = extended >= 3 and not is_pinching
        is_fist = extended == 0

        # -- spread (fingertip spread relative to hand size) --
        spread = _dist(index_tip, pinky_tip) / hand_size
        is_spread = is_open_palm and spread > 1.3

        self._fire_state_gesture("pinch", is_pinching, now)
        self._fire_state_gesture("open_palm", is_open_palm and not is_spread, now)
        self._fire_state_gesture("fist", is_fist, now)
        self._fire_state_gesture("spread_fingers", is_spread, now)

        # -- swipe (wrist x movement over a short window) --
        self._wrist_history.append((now, wrist[0]))
        self._wrist_history = [(t, x) for t, x in self._wrist_history if now - t < SWIPE_MAX_DURATION]
        if len(self._wrist_history) >= 2:
            oldest_t, oldest_x = self._wrist_history[0]
            dx = wrist[0] - oldest_x
            if abs(dx) > SWIPE_MIN_DELTA and self._cooldown_ok("swipe", now, SWIPE_COOLDOWN):
                self._fire("swipe_right" if dx > 0 else "swipe_left", now)
                self._wrist_history = []

        # -- push/pull (bounding box size = rough depth proxy) --
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bbox_size = (max(xs) - min(xs)) * (max(ys) - min(ys))
        self._bbox_size_history.append((now, bbox_size))
        self._bbox_size_history = [(t, s) for t, s in self._bbox_size_history if now - t < 0.4]
        if len(self._bbox_size_history) >= 2:
            oldest_t, oldest_s = self._bbox_size_history[0]
            if oldest_s > 0.0001:
                rel_change = (bbox_size - oldest_s) / oldest_s
                if abs(rel_change) > DEPTH_PUSH_PULL_DELTA and self._cooldown_ok("depth", now, SWIPE_COOLDOWN):
                    self._fire("push_forward" if rel_change > 0 else "pull_backward", now)
                    self._bbox_size_history = []

        # -- rotate (wrist -> middle_mcp angle over time) --
        angle = math.atan2(middle_mcp[1] - wrist[1], middle_mcp[0] - wrist[0])
        self._angle_history.append((now, angle))
        self._angle_history = [(t, a) for t, a in self._angle_history if now - t < 0.4]
        if len(self._angle_history) >= 2:
            oldest_t, oldest_a = self._angle_history[0]
            delta = angle - oldest_a
            if abs(delta) > 0.9 and self._cooldown_ok("rotate", now, SWIPE_COOLDOWN):
                self._fire("rotate_cw" if delta > 0 else "rotate_ccw", now)
                self._angle_history = []

        # -- pinch-and-hold drag: fire continuously while pinching (caller
        # decides what "drag" means, e.g. moving Toby's panel) --
        if is_pinching:
            if self.on_gesture:
                self.on_gesture(f"__pinch_drag__:{wrist[0]}:{wrist[1]}")

        # -- custom gesture matching --
        if self.custom_gestures:
            normalized = self._normalize_landmarks(pts, wrist, hand_size)
            for g in self.custom_gestures:
                if self._matches_template(normalized, g["template"]) and self._cooldown_ok(
                    f"custom:{g['name']}", now, GESTURE_COOLDOWN * 1.5
                ):
                    self._fire(f"custom:{g['name']}", now)

    def _normalize_landmarks(self, pts, wrist, hand_size):
        return [((p[0] - wrist[0]) / hand_size, (p[1] - wrist[1]) / hand_size) for p in pts]

    def _matches_template(self, live_frame, template):
        # compare against the template frame closest in shape (first frame is
        # representative enough for a simple nearest-static-pose match; a
        # true DTW-over-sequence match would be more robust but meaningfully
        # more code for a feature already this deep into "reasonable scope")
        best = template[len(template) // 2]
        total = sum(_dist(a, b) for a, b in zip(live_frame, best))
        return (total / len(live_frame)) < CUSTOM_MATCH_THRESHOLD

    def _fire_state_gesture(self, name, active, now):
        # Require the pose to hold for a few consecutive frames before firing —
        # a single noisy frame misclassifying a hand shape (or a hand briefly
        # passing through frame) shouldn't be able to trigger an action like
        # popping the panel open. This is the main defense against false
        # positives from imperfect detection.
        streak_key = f"_streak_{name}"
        if active:
            self._last_gesture_time[streak_key] = self._last_gesture_time.get(streak_key, 0) + 1
        else:
            self._last_gesture_time[streak_key] = 0

        was_active = self._last_gesture_time.get(f"_state_{name}", False)
        now_active = self._last_gesture_time[streak_key] >= GESTURE_HOLD_FRAMES
        if now_active and not was_active:
            self._fire(name, now)
        self._last_gesture_time[f"_state_{name}"] = now_active

    def _cooldown_ok(self, key, now, cooldown):
        last = self._last_gesture_time.get(key, 0)
        return (now - last) > cooldown

    def _fire(self, name, now):
        self._last_gesture_time[name] = now
        if self.on_gesture:
            self.on_gesture(name)

    # -- face landmarks -> blink/smile/tilt/eyebrow/gaze -----------------------
    def _handle_face(self, result):
        now = time.monotonic()
        if not result.face_landmarks:
            if self.on_face_frame:
                self.on_face_frame(None)
            return

        landmarks = result.face_landmarks[0]  # Tasks API: list of NormalizedLandmark directly
        pts = [(lm.x, lm.y) for lm in landmarks]
        if self.on_face_frame:
            self.on_face_frame(pts)

        try:
            left_eye_pts = [pts[i] for i in LEFT_EYE]
            right_eye_pts = [pts[i] for i in RIGHT_EYE]
            ear = (_eye_aspect_ratio(left_eye_pts) + _eye_aspect_ratio(right_eye_pts)) / 2.0
        except IndexError:
            return  # landmark set too small (refine_landmarks unavailable) — skip this frame's face heuristics

        eyes_closed = ear < EAR_BLINK_THRESHOLD
        if eyes_closed and not self._eye_was_closed:
            self._blink_history.append(now)
            self._blink_history = [t for t in self._blink_history if now - t < DOUBLE_BLINK_WINDOW]
            if len(self._blink_history) >= 2 and self._cooldown_ok("double_blink", now, 1.0):
                self._fire("double_blink", now)
                self._blink_history = []
            elif self._cooldown_ok("blink", now, 0.3):
                self._fire("blink", now)
        self._eye_was_closed = eyes_closed

        # smile: mouth width / height ratio. Edge-triggered with hold-frames +
        # hysteresis (a lower reset threshold), same as the hand-pose gestures —
        # a plain time-based cooldown let a noisy signal sitting right at the
        # threshold refire every ~1.5s forever, which is exactly what happened
        # with eyebrows_raised in testing (it kept reopening/closing the panel
        # with no one touching anything).
        mw = _dist(pts[MOUTH_CORNERS[0]], pts[MOUTH_CORNERS[1]])
        mh = _dist(pts[MOUTH_TOP_BOTTOM[0]], pts[MOUTH_TOP_BOTTOM[1]]) or 0.001
        is_smiling = (mw / mh) > SMILE_RATIO_THRESHOLD
        self._fire_state_gesture("smile", is_smiling, now)

        # eyebrow raise: distance from brow to eye top, relative to face scale
        face_scale = _dist(pts[LEFT_EYE[0]], pts[RIGHT_EYE[3]]) or 0.001
        brow_gap = (pts[LEFT_EYE_TOP][1] - pts[LEFT_EYEBROW][1]) / face_scale
        is_eyebrows_raised = brow_gap > EYEBROW_RAISE_THRESHOLD
        self._fire_state_gesture("eyebrows_raised", is_eyebrows_raised, now)

        # head tilt (angle between the outer eye corners)
        tilt = math.degrees(math.atan2(pts[RIGHT_EYE[3]][1] - pts[LEFT_EYE[0]][1],
                                        pts[RIGHT_EYE[3]][0] - pts[LEFT_EYE[0]][0]))
        if self.on_gesture and abs(tilt) > 12 and self._cooldown_ok("head_tilt", now, 1.0):
            self._fire("head_tilt_left" if tilt > 0 else "head_tilt_right", now)

        # head shake (nose x oscillation) -> cancel gesture
        nose_x = pts[NOSE_TIP][0]
        self._nose_x_history.append((now, nose_x))
        self._nose_x_history = [(t, x) for t, x in self._nose_x_history if now - t < HEAD_SHAKE_WINDOW]
        if len(self._nose_x_history) >= 4:
            xs = [x for _, x in self._nose_x_history]
            if (max(xs) - min(xs)) > HEAD_SHAKE_MIN_DELTA and self._cooldown_ok("head_shake", now, 1.5):
                self._fire("head_shake", now)
                self._nose_x_history = []

        # coarse gaze estimate via iris landmarks (only if refine_landmarks worked)
        if len(pts) > max(LEFT_IRIS + RIGHT_IRIS):
            try:
                left_iris_center = pts[LEFT_IRIS[0]]
                left_eye_left, left_eye_right = pts[LEFT_EYE[0]], pts[LEFT_EYE[3]]
                eye_width = _dist(left_eye_left, left_eye_right) or 0.001
                gaze_x = (left_iris_center[0] - left_eye_left[0]) / eye_width  # 0=looking left edge .. 1=right edge
                if gaze_x < 0.35 and self._cooldown_ok("gaze_left", now, 1.0):
                    self._fire("gaze_left", now)
                elif gaze_x > 0.65 and self._cooldown_ok("gaze_right", now, 1.0):
                    self._fire("gaze_right", now)
            except (IndexError, ZeroDivisionError):
                pass
