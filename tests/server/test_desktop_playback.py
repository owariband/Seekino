from server.desktop.playback import PlaybackDecoder
from tests.server.test_desktop_pipeline import _fixture_movie


def test_paused_seek_uses_the_frame_covering_the_target(tmp_path):
    source = tmp_path / "slow-pan.mp4"
    _fixture_movie(source)
    decoder = PlaybackDecoder(str(source), rgb=True)

    def first_picture(target):
        decoder.seek(target)
        while (sample := decoder.next_sample()) is not None:
            if sample[0] == 0:
                return sample[1], bytes(sample[4])
        raise AssertionError("expected a decoded picture")

    try:
        _, previous = first_picture(200)
        _, following = first_picture(300)
        assert previous != following
        when, picture = first_picture(245)
        assert when == 245_000 and picture == previous
        # The final displayed frame also covers seeks after its own PTS.
        _, last = first_picture(1900)
        assert first_picture(1999) == (1_999_000, last)
    finally:
        decoder.close()
