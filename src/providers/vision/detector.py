"""
Vision Detector — YOLOv8 object detection + MediaPipe pose estimation.

All heavy imports are guarded by try/except so the module remains
importable even when optional dependencies are missing.
"""

import logging
from typing import Any, Dict, List, Optional

from lyra.core.config import ConfigManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VisionDetector
# ---------------------------------------------------------------------------


class VisionDetector:
    """Wraps YOLOv8 (Ultralytics) for object detection and MediaPipe
    for pose estimation."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._yolo_model = None
        self._pose_model = None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if at least one backend (YOLO / MediaPipe)
        could be loaded."""
        if self._yolo_model is not None or self._pose_model is not None:
            return True
        # Probe imports
        yolo_ok = False
        mediapipe_ok = False
        try:
            import ultralytics  # noqa: F401
            yolo_ok = True
        except ImportError:
            pass
        try:
            import mediapipe  # noqa: F401
            mediapipe_ok = True
        except ImportError:
            pass
        return yolo_ok or mediapipe_ok

    # ------------------------------------------------------------------
    # YOLO model lazy-load
    # ------------------------------------------------------------------

    def _ensure_yolo(self) -> bool:
        if self._yolo_model is not None:
            return True
        try:
            from ultralytics import YOLO  # type: ignore[import-untyped]
            model_path = self.config.get("YOLO_MODEL_PATH", "yolov8n.pt")
            self._yolo_model = YOLO(model_path)
            logger.info("VisionDetector: YOLO model loaded (%s)", model_path)
            return True
        except Exception:
            logger.warning("VisionDetector: YOLO not available")
            return False

    # ------------------------------------------------------------------
    # MediaPipe pose model lazy-load
    # ------------------------------------------------------------------

    def _ensure_pose(self) -> bool:
        if self._pose_model is not None:
            return True
        try:
            import mediapipe as mp  # type: ignore[import-untyped]
            self._pose_model = mp.solutions.pose.Pose(
                static_image_mode=True,
                model_complexity=1,
                min_detection_confidence=0.5,
            )
            logger.info("VisionDetector: MediaPipe Pose model loaded")
            return True
        except Exception:
            logger.warning("VisionDetector: MediaPipe Pose not available")
            return False

    # ------------------------------------------------------------------
    # Object detection (YOLOv8)
    # ------------------------------------------------------------------

    async def detect_objects(self, image_path: str) -> List[Dict[str, Any]]:
        """Run YOLOv8 inference on *image_path*.

        Returns a list of dicts::

            [
                {
                    "label": "person",
                    "confidence": 0.92,
                    "bbox": [x1, y1, x2, y2],   # normalised 0‑1
                },
                ...
            ]

        Returns an empty list if YOLO is unavailable or inference fails.
        """
        if not self._ensure_yolo():
            return []

        try:
            results = self._yolo_model(image_path)  # type: ignore[union-attr]
            detections = []
            for r in results:
                boxes = r.boxes
                if boxes is None:
                    continue
                for box in boxes:
                    detections.append(
                        {
                            "label": r.names[int(box.cls[0])],
                            "confidence": float(box.conf[0]),
                            "bbox": [float(v) for v in box.xyxyn[0].tolist()],
                        }
                    )
            return detections
        except Exception:
            logger.exception("VisionDetector: detect_objects failed")
            return []

    # ------------------------------------------------------------------
    # Pose estimation (MediaPipe)
    # ------------------------------------------------------------------

    async def estimate_pose(self, image_path: str) -> List[Dict[str, Any]]:
        """Run MediaPipe Pose landmark detection on *image_path*.

        Returns a list of landmarks dicts::

            [
                {
                    "landmark": 0,       # index (0‑32)
                    "x": 0.5,
                    "y": 0.3,
                    "z": 0.1,
                    "visibility": 0.99,
                },
                ...
            ]

        Returns an empty list if MediaPipe is unavailable or inference
        fails.
        """
        if not self._ensure_pose():
            return []

        try:
            import cv2  # type: ignore[import-untyped]

            image = cv2.imread(image_path)
            if image is None:
                logger.warning("VisionDetector: cannot read image — %s", image_path)
                return []

            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            result = self._pose_model.process(rgb)  # type: ignore[union-attr]

            if result.pose_landmarks is None:
                return []

            landmarks = []
            for idx, lm in enumerate(result.pose_landmarks.landmark):
                landmarks.append(
                    {
                        "landmark": idx,
                        "x": lm.x,
                        "y": lm.y,
                        "z": lm.z,
                        "visibility": lm.visibility,
                    }
                )
            return landmarks
        except Exception:
            logger.exception("VisionDetector: estimate_pose failed")
            return []
