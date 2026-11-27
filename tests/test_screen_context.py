import base64

from PIL import Image

from modules import screen_context as sc


class _Resp:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"response": self._text}


def test_big_screens_are_downscaled_and_small_ones_are_not():
    big = Image.new("RGB", (1920, 1080))
    assert max(sc.downscale(big).size) == sc.MAX_SIDE
    small = Image.new("RGB", (400, 300))
    assert sc.downscale(small).size == (400, 300)


def test_one_look_sends_one_image_to_the_vision_model_and_returns_text():
    calls = {}

    def post(url, json=None, timeout=None):
        calls.update(url=url, body=json)
        return _Resp("A traceback in a terminal.")

    out = sc.capture_screen_context("what's wrong?", grab=lambda: b"jpegbytes", post=post)
    assert out == "A traceback in a terminal."
    assert calls["url"].endswith("/api/generate")
    assert calls["body"]["model"] == sc.VISION_MODEL
    assert calls["body"]["images"] == [base64.b64encode(b"jpegbytes").decode("ascii")]
    assert calls["body"]["keep_alive"] == "30s" and calls["body"]["think"] is False
    assert len(calls["body"]["images"]) == 1


def test_the_question_is_capped():
    seen = {}

    def post(url, json=None, timeout=None):
        seen["prompt"] = json["prompt"]
        return _Resp("ok")

    sc.capture_screen_context("x" * 2000, grab=lambda: b"j", post=post)
    assert len(seen["prompt"]) < 700
