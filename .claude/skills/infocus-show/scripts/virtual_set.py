"""The clean virtual sets the anchors are re-composited onto: each theme's Left/Middle/Right set image, where the
table and the seats are on it, and which set a recording was filmed on.

The switcher keys the anchors onto the set pixel for pixel, so a recorded frame equals its set image wherever no
anchor is. That's how the matte finds the set behind the anchor, and how the set a show was filmed on is told apart.
"""
import os

import cv2
import numpy as np

from common import THEMES

SET_KEY = {"L": "left", "M": "middle", "R": "right"}

# Measured on the 2026 sets (the October shows). A theme whose table differs gets its own entry in GEOMETRY; the
# others use the regular one.
# Where the anchors' faces sit when a camera is framed right: face centre x per anchor (left to right), face centre
# y, face height.
SEATS = {"M": {"x": [585, 1355], "y": 320, "h": 215},
         "L": {"x": [1500], "y": 330, "h": 252},
         "R": {"x": [490], "y": 320, "h": 254}}
# Back edge of each set's table (where the cabinet meets the table top), as (x, y) points along its curve. Each
# anchor's waist goes onto this edge; their arms and hands rest on the table top in front of it.
DESK_BACK = {"M": [(0, 960), (200, 935), (360, 922), (520, 913), (680, 907), (840, 904), (1000, 903), (1160, 905),
                   (1320, 910), (1480, 917), (1640, 928), (1800, 944), (1920, 958)],
             "L": [(0, 945), (320, 935), (640, 925), (960, 930), (1280, 940), (1600, 965), (1920, 1000)],
             "R": [(0, 990), (160, 977), (320, 960), (640, 940), (960, 922), (1280, 920), (1600, 932), (1920, 942)]}
# The table's front lip (its light rounded edge). The front panel below it is drawn over the anchors, so a body the
# camera cut off at the bottom of the frame never shows a hard edge on the table top.
DESK_LIP = {"M": [(0, 1085), (160, 1073), (320, 1059), (480, 1050), (640, 1043), (800, 1040), (960, 1038),
                  (1120, 1042), (1280, 1043), (1440, 1048), (1600, 1058), (1760, 1071), (1920, 1085)],
            "L": [(0, 983), (160, 974), (320, 967), (480, 964), (640, 964), (800, 967), (960, 974), (1120, 984),
                  (1280, 998), (1440, 1015), (1600, 1036), (1760, 1059), (1920, 1080)],
            "R": [(0, 1080), (160, 1054), (320, 1030), (480, 1010), (640, 993), (800, 980), (960, 970), (1120, 963),
                  (1280, 960), (1440, 960), (1600, 963), (1760, 969), (1920, 978)]}
GEOMETRY = {"": {cam: {"seats": SEATS[cam], "desk_back": DESK_BACK[cam], "desk_lip": DESK_LIP[cam]}
                 for cam in SET_KEY}}


def set_path(cam, theme, package):
    return os.path.join(package, THEMES[theme][SET_KEY[cam]])


def set_image(cam, theme, package):
    """The theme's set for a camera (L, M or R) as 1920x1080 RGB."""
    path = set_path(cam, theme, package)
    img = cv2.imread(path) if os.path.exists(path) else None
    if img is None:
        raise RuntimeError(f"No {cam} set for theme '{theme}' ({path})")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def geometry(cam, theme):
    """Seats, table back edge and front lip for a camera's set in a theme."""
    return GEOMETRY.get(theme, GEOMETRY[""])[cam]


def installed_themes(package):
    """Themes whose Left, Middle and Right sets are all in the package."""
    return [t for t in THEMES if all(os.path.exists(set_path(cam, t, package)) for cam in SET_KEY)]


def recorded_theme(frame, mask, cam, package):
    """The installed theme whose set the frame was keyed onto: the closest match where no anchor is (`mask` False)."""
    keep = ~mask
    best, theme = None, ""
    for t in installed_themes(package):
        diff = np.abs(frame.astype(np.int16) - set_image(cam, t, package).astype(np.int16)).max(2)
        score = float(np.median(diff[keep]))
        if best is None or score < best:
            best, theme = score, t
    return theme
