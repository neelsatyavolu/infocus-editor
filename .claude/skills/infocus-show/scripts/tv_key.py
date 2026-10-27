"""The switcher's blue key on the side-set TVs (numpy only).

The Left/Right virtual sets carry a flat #0047BB screen that the TriCaster fills with the TV Standby or a card. A
show filmed with the TVs left unfilled shows that blue instead: the analysis treats it like the standby (a TV with
no card), and the anchor foregrounds key it out of their soft edges so the fresh card shows through.
"""
import numpy as np

KEY = (0, 71, 187)                     # the sets' TV screen color, regular and Halloween
KEY_LEAD = KEY[2] - max(KEY[:2])       # how far the key's blue rises above its red and green


def blueness(f):
    """0..1 per pixel: how much blue rises above red and green, relative to the key's own lead (1 = key blue)."""
    f = f.astype(np.int16)
    return np.clip((f[..., 2] - f[..., :2].max(-1)) / KEY_LEAD, 0, 1).astype(np.float32)


def key_mask(f):
    """Pixels showing the blue key (compression shades included; navy, denim and the standby stay out)."""
    return (blueness(f) > 0.6) & (f[..., 2] > 120)


def on_key(frame, rect, share=0.3):
    """Whether a TV screen (x, y, w, h) shows the blue key in this frame, rather than the standby or a card. Keying is
    done only then, so a blue shirt crossing a standby TV keeps its edge."""
    x, y, w, h = rect
    return bool(key_mask(frame[y:y + h, x:x + w]).mean() > share)


def despill(f, alpha):
    """Pull blue spill out of soft-edge pixels (0 < alpha < 1): blue no higher than the brighter of red and green.
    Solid and empty pixels are returned untouched."""
    out = f.copy()
    edge = (alpha > 0) & (alpha < 1)
    b, rg = out[..., 2], out[..., :2].max(-1)
    out[..., 2] = np.where(edge, np.minimum(b, rg), b)
    return out
