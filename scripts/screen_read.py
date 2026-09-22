"""
screen_read.py — screenshot + OCR, so the assistant can actually see what's
on your screen instead of guessing.

Requires: grim (screenshot), tesseract (OCR) — both CLI tools, no Python
bindings needed, so no extra pip dependencies.

Setup:
  sudo pacman -S tesseract tesseract-data-eng
"""

import subprocess
import tempfile
from pathlib import Path


def capture_screen_text() -> str:
    """Takes a full-screen screenshot and OCRs it. Returns extracted text,
    or a short error string if grim/tesseract aren't available."""
    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / "screen.png"

        try:
            subprocess.run(["grim", str(img_path)], check=True, timeout=10)
        except FileNotFoundError:
            return "[grim not installed — can't take a screenshot]"
        except subprocess.CalledProcessError as e:
            return f"[grim failed: {e}]"
        except subprocess.TimeoutExpired:
            return "[grim timed out taking screenshot]"

        try:
            result = subprocess.run(
                ["tesseract", str(img_path), "stdout"],
                check=True, timeout=20,
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            return "[tesseract not installed — can't read screen text]"
        except subprocess.CalledProcessError as e:
            return f"[tesseract failed: {e}]"
        except subprocess.TimeoutExpired:
            return "[tesseract timed out reading screen]"

        text = result.stdout.strip()
        return text if text else "[no readable text found on screen]"
