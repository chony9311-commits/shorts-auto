import pytest

from app import scraper

JSONLD = """<html><head><title>접이식 정리함 - 쿠팡!</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"접이식 대용량 정리함 3개입",
 "description":"뚜껑까지 접히는 간편한 수납. 방수 원단이라 물세척이 쉬워요. 무료배송 쿠폰 받기. 가벼워서 휴대하기 좋은 손잡이형 디자인",
 "brand":{"@type":"Brand","name":"모아홈"},
 "image":["/img/a.jpg","https://cdn.x.com/b.jpg"],
 "offers":{"@type":"Offer","price":"12900","priceCurrency":"KRW"},
 "video":{"@type":"VideoObject","contentUrl":"https://cdn.x.com/v.mp4"}}
</script></head><body><h1>접이식 대용량 정리함 3개입</h1>
<ul><li>구매하기</li><li>스테인리스 프레임으로 튼튼하게 오래 사용</li><li>로그인이 필요합니다</li></ul>
<p>""" + "x" * 700 + """</p></body></html>"""

OG_ONLY = """<html><head>
<meta property="og:title" content="무선 핸디 선풍기 : 네이버 쇼핑">
<meta property="og:description" content="저소음 BLDC 모터로 조용하게. 한 번 충전으로 8시간 사용 · 접이식 거치대 포함">
<meta property="og:image" content="https://shop.pstatic.net/p.jpg">
<meta property="product:price:amount" content="19,800">
</head><body>""" + "y" * 700 + "</body></html>"


def test_jsonld_product():
    p = scraper.parse_product_html(JSONLD, "https://www.coupang.com/vp/products/1")
    assert p.name == "접이식 대용량 정리함 3개입"
    assert p.price == 12900 and p.price_text == "12,900원"
    assert p.brand == "모아홈"
    assert p.platform == "coupang"
    assert p.image_urls[0] == "https://www.coupang.com/img/a.jpg"
    assert p.video_urls == ["https://cdn.x.com/v.mp4"]
    assert 3 <= len(p.features) <= 5
    joined = " ".join(p.features)
    assert "방수" in joined and "스테인리스" in joined
    assert "무료배송" not in joined and "구매하기" not in joined and "로그인" not in joined


def test_og_only_naver():
    p = scraper.parse_product_html(OG_ONLY, "https://smartstore.naver.com/x/products/2")
    assert p.name == "무선 핸디 선풍기"
    assert p.price == 19800
    assert p.platform == "naver"
    assert p.image_urls == ["https://shop.pstatic.net/p.jpg"]
    assert any("저소음" in f for f in p.features)


def test_no_name_raises():
    with pytest.raises(scraper.ScrapeError):
        scraper.parse_product_html("<html><body>" + "z" * 800 + "</body></html>", "https://a.com")


def test_blocked_is_propagated():
    def blocked(url):
        raise scraper.ScrapeBlocked("막힘")
    with pytest.raises(scraper.ScrapeBlocked):
        scraper.fetch_product("https://www.coupang.com/vp/products/1", fetcher=blocked)


def test_fetch_product_keeps_user_url():
    p = scraper.fetch_product("https://link.coupang.com/a/abc",
                              fetcher=lambda u: ("https://www.coupang.com/vp/products/1", JSONLD))
    assert p.url == "https://link.coupang.com/a/abc" and p.platform == "coupang"


def test_bad_url():
    with pytest.raises(scraper.ScrapeError):
        scraper.fetch_product("not a url")


def test_manual_fallback():
    p = scraper.product_from_manual("미니 믹서기", "원터치 세척, USB 충전\n컵 2개 구성 / 350ml", price="15,900원")
    assert p.manual and p.platform == "manual" and p.price == 15900
    assert p.features == ["원터치 세척", "USB 충전", "컵 2개 구성", "350ml"]
    with pytest.raises(scraper.ScrapeError):
        scraper.product_from_manual("  ")


def test_extract_urls():
    t = "이거 봐줘 https://link.coupang.com/a/x1, 그리고 (https://smartstore.naver.com/a/products/3). 끝"
    assert scraper.extract_urls(t) == ["https://link.coupang.com/a/x1", "https://smartstore.naver.com/a/products/3"]


def test_price_parsing():
    assert scraper.parse_price("1,299,000원")[0] == 1299000
    assert scraper.parse_price(None, "19800")[0] == 19800
    assert scraper.parse_price("무료")[0] is None
