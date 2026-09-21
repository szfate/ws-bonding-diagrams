"""Unit tests for export_die_maps.py coordinate conversion."""

from export_die_maps import pad_px_coords

# 100 x 50 um die rendered at 200 x 100 px → 2 px per um, both axes.
DIE_BB = (0.0, 0.0, 100.0, 50.0)


def test_center_and_rect_at_known_coordinates():
    px = pad_px_coords(DIE_BB, 200, 100,
                       cx=25.0, cy=10.0,
                       x0=10.0, x1=30.0, y0=10.0, y1=20.0)
    # x: (25 - 0) * 2 = 50
    assert px["cx_px"] == 50.0
    # y flipped: (50 - 10) * 2 = 80 (top-left origin)
    assert px["cy_px"] == 80.0
    assert px["x0_px"] == 20.0
    assert px["x1_px"] == 60.0
    # rect y0_um..y1_um (10..20) maps to 60..80 (top..bottom)
    assert px["y0_px"] == 60.0
    assert px["y1_px"] == 80.0


def test_nonzero_die_origin():
    # Die bounding box offset from GDS origin: mapping is relative to die_bb.
    bb = (1000.0, 2000.0, 1100.0, 2050.0)
    px = pad_px_coords(bb, 200, 100,
                       cx=1050.0, cy=2050.0,
                       x0=1040.0, x1=1060.0,
                       y0=2040.0, y1=2050.0)
    assert px["cx_px"] == 100.0
    # top edge of die (y=2050) is image row 0
    assert px["cy_px"] == 0.0
    # rect y0_um..y1_um (2040..2050) maps to 0..20 (top..bottom)
    assert px["y0_px"] == 0.0
    assert px["y1_px"] == 20.0


from export_die_maps import build_die_json

PAD = {
    "n": 1, "edge": "T", "net": "vdd",
    "x0_um": 10.0, "y0_um": 10.0, "x1_um": 30.0, "y1_um": 20.0,
    "cx_um": 25.0, "cy_um": 15.0,
}


def test_build_die_json_schema():
    record = {
        "name": "AS01_chip_top_6_0", "code": "AS01", "reticle": "ws-run1",
        "die_bb_um": [0.0, 0.0, 100.0, 50.0],
        "slot_size": "1x1",
        "pads": [PAD],
    }
    j = build_die_json(record, 200, 100)
    assert j["design"] == "AS01_chip_top_6_0"
    assert j["reticle"] == "ws-run1"
    assert j["slot_size"] == "1x1"
    assert j["pad_count"] == {"total": 1, "top": 1, "bottom": 0,
                              "left": 0, "right": 0, "labelled": 1}
    assert j["image"] == {"file": "AS01.png", "width_px": 200,
                          "height_px": 100, "px_per_um": 2.0}
    assert j["die"] == {"width_um": 100.0, "height_um": 50.0}
    p = j["pads"][0]
    assert p["number"] == 1
    assert p["edge"] == "T"
    assert p["net"] == "vdd"
    assert (p["cx_px"], p["cy_px"]) == (50.0, 70.0)
    assert (p["x0_px"], p["x1_px"], p["y0_px"], p["y1_px"]) == (20.0, 60.0, 60.0, 80.0)
    assert p["cx_um"] == 25.0 and p["cy_um"] == 15.0
