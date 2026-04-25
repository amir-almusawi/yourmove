"""Mock heavy edge dependencies that aren't installed in the server Python env."""
import sys
from unittest.mock import MagicMock

for mod in ["cv2", "numpy", "torch", "torchvision", "PIL", "transformers"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
