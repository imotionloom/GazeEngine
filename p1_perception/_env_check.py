import sys
print("python", sys.version.split()[0])
import cv2; print("cv2", cv2.__version__)
import mediapipe as mp; print("mediapipe", mp.__version__)
from mediapipe.tasks.python import vision; print("mediapipe.tasks ok")
import insightface; print("insightface", insightface.__version__)
import numpy as np; print("numpy", np.__version__)
