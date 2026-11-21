from app import fonts, settings, subtitles


def test_catalog_resolve_and_bold(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FONT_DIR", tmp_path)
    assert fonts.resolve("jua") is None and not fonts.public_list()[0]["installed"]
    (tmp_path / "BlackHanSans-Regular.ttf").write_bytes(b"x" * 20)
    r = fonts.resolve("blackhansans")
    assert r["family"] == "Black Han Sans" and r["bold"] is False
    out = subtitles.build_preset_ass("reels_clean", [{"start": 0, "end": 2, "text": "진짜 좋아"}],
                                     tmp_path / "a.ass", font=r["family"], bold=r["bold"])
    txt = out.read_text(encoding="utf-8")
    assert "Black Han Sans" in txt and ",&H96000000,0,0,0,0," in txt
    out2 = subtitles.build_preset_ass("reels_clean", [{"start": 0, "end": 2, "text": "진짜 좋아"}], tmp_path / "b.ass")
    assert ",&H96000000,-1,0," in out2.read_text(encoding="utf-8")
