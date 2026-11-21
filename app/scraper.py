"""상품 링크 파서.

쿠팡 / 네이버(스마트스토어·브랜드스토어·쇼핑) / 일반 쇼핑몰 URL에서
  상품명 · 가격 · 핵심 특장점 3~5줄 · 대표 이미지 · 상세 영상 URL
을 뽑는다.

쿠팡·네이버는 봇 접근을 막는 경우가 많다. 막히면 ScrapeBlocked 를 던지고,
호출하는 쪽은 product_from_manual()(상품명 + 특징 텍스트 직접 입력)로 폴백한다.
파서(parse_product_html)는 네트워크와 분리된 순수 함수라서 테스트하기 쉽다.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s<>\"'，、]+", re.I)


class ScrapeError(RuntimeError):
    """상품 정보를 못 읽었다(사용자에게 그대로 보여줄 수 있는 메시지)."""


class ScrapeBlocked(ScrapeError):
    """사이트가 자동 접근을 막았다 -> 직접 입력 폴백을 안내한다."""


@dataclass
class ProductInfo:
    name: str
    price: Optional[int] = None            # 원 단위 정수
    price_text: str = ""                   # "12,900원"
    features: List[str] = field(default_factory=list)   # 핵심 특장점 3~5줄
    image_urls: List[str] = field(default_factory=list)
    video_urls: List[str] = field(default_factory=list)
    url: str = ""
    platform: str = "generic"              # coupang | naver | generic | manual
    brand: str = ""
    manual: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ URL 도우미
def extract_urls(text: str) -> List[str]:
    """메시지/텍스트 안의 URL을 순서대로(중복 제거) 뽑는다. 끝에 붙은 문장부호는 뗀다."""
    seen: List[str] = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}>'\"")
        if u not in seen:
            seen.append(u)
    return seen


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("coupang.com") or host.endswith("coupa.ng"):
        return "coupang"
    if host.endswith("naver.com") or host.endswith("naver.me"):
        return "naver"
    return "generic"


# ------------------------------------------------------------------ 네트워크
_BLOCK_MARKERS = ("access denied", "captcha", "are you a robot", "보안 확인", "비정상적인 접근",
                  "자동입력 방지", "서비스 접속이 불가")


def fetch_html(url: str, timeout: float = 12.0) -> Tuple[str, str]:
    """(최종 URL, HTML). 차단/오류는 ScrapeBlocked·ScrapeError."""
    try:
        import requests  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise ScrapeError("requests 가 설치되어 있지 않습니다. 터미널: pip install requests") from exc
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
                     "Accept": "text/html,application/xhtml+xml"},
        )
    except Exception as exc:  # noqa: BLE001
        raise ScrapeError(f"페이지를 불러오지 못했습니다(인터넷 연결·주소 확인): {exc}") from exc
    if resp.status_code in (401, 403, 407, 429, 451, 503):
        raise ScrapeBlocked(f"사이트가 자동 접근을 막았습니다(HTTP {resp.status_code}).")
    if resp.status_code >= 400:
        raise ScrapeError(f"페이지를 열 수 없습니다(HTTP {resp.status_code}).")
    resp.encoding = resp.encoding or resp.apparent_encoding or "utf-8"
    html = resp.text
    low = html[:4000].lower()
    if len(html) < 600 or any(mk in low for mk in _BLOCK_MARKERS):
        raise ScrapeBlocked("사이트가 보안 확인 페이지를 보여줬습니다.")
    return str(resp.url), html


# ------------------------------------------------------------------ 텍스트 정리
_SITE_SUFFIX = re.compile(r"\s*[-|:·–—]\s*(쿠팡!?|COUPANG|네이버\s*쇼핑|네이버|NAVER|스마트스토어|"
                          r"브랜드스토어|G마켓|옥션|11번가|SSG\.COM|무신사)\s*$", re.I)
_PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{3,9})\s*원")
_NOISE = re.compile(
    r"배송|무료|쿠폰|로그인|회원|장바구니|리뷰|후기|평점|적립|카드|할부|반품|교환|환불|판매자|고시|"
    r"사업자|통신판매|개인정보|이용약관|고객센터|문의|공지|다운로드|앱\s*설치|©|copyright|"
    r"검색|카테고리|바로구매|구매하기|찜|공유|신고|QnA|Q&A|상품번호|제조국|원산지|KC\s*인증|"
    r"주문|결제|혜택|이벤트|포인트|최저가|최대\s*\d+%|\d+\s*%\s*할인", re.I)
_FEATURE_HINT = re.compile(
    r"방수|무선|휴대|접이|수납|용량|간편|가벼|가볍|튼튼|세척|충전|보온|보냉|저소음|무소음|강력|"
    r"편리|편안|부드|미끄|흡수|속건|통풍|냄새|탈취|정리|설치|조절|자동|스마트|고급|프리미엄|"
    r"안전|친환경|내구|두께|사이즈|색상|국내|한정|대용량|초경량|스테인|실리콘|항균|살균", re.I)


def _clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def clean_title(title: str) -> str:
    t = _clean_text(title)
    for _ in range(2):
        t = _SITE_SUFFIX.sub("", t)
    return t.strip(" -|:·")


def parse_price(*cands: Any) -> Tuple[Optional[int], str]:
    for c in cands:
        if c is None or c == "":
            continue
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            v = int(round(float(c)))
            if v > 0:
                return v, f"{v:,}원"
            continue
        s = str(c)
        m = _PRICE_RE.search(s)
        if m:
            v = int(m.group(1).replace(",", ""))
            return v, f"{v:,}원"
        digits = re.fullmatch(r"\s*(\d[\d,]*)(?:\.\d+)?\s*", s)
        if digits:
            v = int(digits.group(1).replace(",", ""))
            if v > 0:
                return v, f"{v:,}원"
    return None, ""


def _split_feature_candidates(texts: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in texts:
        for piece in re.split(r"[\r\n]+|[•·▪■●◆▶✔✅★☆※]|(?<=[.!?。])\s+|\s[|/]\s|;", raw or ""):
            p = _clean_text(piece).strip(" -–—,.:)(")
            if p:
                out.append(p)
    return out


def pick_features(candidates: Iterable[str], name: str = "", limit: int = 5) -> List[str]:
    """후보 문장에서 광고 소음을 거르고 특장점 줄을 고른다."""
    scored: List[Tuple[int, int, str]] = []
    seen_keys: set = set()
    name_key = re.sub(r"\s+", "", name)
    for i, cand in enumerate(_split_feature_candidates(candidates)):
        if not (6 <= len(cand) <= 48):
            continue
        if not re.search(r"[가-힣]", cand):
            continue
        if _NOISE.search(cand):
            continue
        key = re.sub(r"\s+", "", cand)
        if key == name_key or key in name_key or key in seen_keys:
            continue
        seen_keys.add(key)
        score = 2 if _FEATURE_HINT.search(cand) else 0
        if len(cand) <= 28:
            score += 1
        scored.append((-score, i, cand))
    scored.sort()
    return [c for _, _, c in scored[:limit]]


# ------------------------------------------------------------------ 파싱 (순수 함수)
def _iter_jsonld(soup: Any) -> Iterable[Dict[str, Any]]:
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                if "@graph" in node and isinstance(node["@graph"], list):
                    stack.extend(node["@graph"])
                yield node
            elif isinstance(node, list):
                stack.extend(node)


def _is_type(node: Dict[str, Any], want: str) -> bool:
    t = node.get("@type")
    return (t == want) or (isinstance(t, list) and want in t)


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _abs(base: str, u: Any) -> Optional[str]:
    if isinstance(u, dict):
        u = u.get("url") or u.get("contentUrl")
    if not isinstance(u, str) or not u.strip():
        return None
    full = urljoin(base, u.strip())
    return full if full.startswith(("http://", "https://")) else None


def _meta(soup: Any, *keys: str) -> str:
    for k in keys:
        tag = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def parse_product_html(html: str, url: str = "") -> ProductInfo:
    """HTML 한 장 -> ProductInfo. 이름을 못 찾으면 ScrapeError."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise ScrapeError("beautifulsoup4 가 필요합니다. 터미널: pip install beautifulsoup4 lxml") from exc
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    platform = detect_platform(url)
    name = brand = desc = ""
    price_raw: Any = None
    images: List[str] = []
    videos: List[str] = []

    for node in _iter_jsonld(soup):
        if _is_type(node, "Product"):
            name = name or _clean_text(str(node.get("name") or ""))
            desc = desc or str(node.get("description") or "")
            b = node.get("brand")
            brand = brand or _clean_text(str(b.get("name") if isinstance(b, dict) else (b or "")))
            for im in _as_list(node.get("image")):
                u = _abs(url, im)
                if u:
                    images.append(u)
            for off in _as_list(node.get("offers")):
                if isinstance(off, dict):
                    price_raw = price_raw or off.get("price") or off.get("lowPrice")
            for v in _as_list(node.get("video")):
                u = _abs(url, v)
                if u:
                    videos.append(u)
        elif _is_type(node, "VideoObject"):
            u = _abs(url, node.get("contentUrl") or node.get("embedUrl"))
            if u:
                videos.append(u)

    name = name or clean_title(_meta(soup, "og:title", "twitter:title")) or clean_title(
        soup.title.get_text() if soup.title else "")
    for sel in ("h1.prod-buy-header__title", "h1._22kNQuEXmb", "h3._22kNQuEXmb", "h1"):
        if name:
            break
        el = soup.select_one(sel)
        if el:
            name = clean_title(el.get_text())
    name = clean_title(name)

    price_raw = price_raw or _meta(soup, "product:price:amount", "og:price:amount", "twitter:data1")
    price, price_text = parse_price(price_raw)
    if price is None:
        for sel in ("span.total-price strong", ".prod-sale-price .total-price", "strong._1LY7DqCnwR",
                    "[class*=price] strong", "[class*=Price]"):
            el = soup.select_one(sel)
            if el:
                price, price_text = parse_price(el.get_text())
                if price:
                    break
    if price is None:
        price, price_text = parse_price(_meta(soup, "og:description", "description"))

    og_img = _abs(url, _meta(soup, "og:image", "twitter:image"))
    if og_img:
        images.insert(0, og_img)
    og_vid = _abs(url, _meta(soup, "og:video", "og:video:url", "og:video:secure_url"))
    if og_vid:
        videos.insert(0, og_vid)
    for v in soup.find_all(["video", "source"]):
        u = _abs(url, v.get("src"))
        if u and re.search(r"\.(mp4|webm|m3u8|mov)(\?|$)", u, re.I):
            videos.append(u)

    cands: List[str] = [desc, _meta(soup, "og:description", "description")]
    for sel in (".prod-description", ".prod-attr-item", ".prod-buy-header__features li",
                "[class*=feature] li", "[class*=Feature] li", "[class*=spec] li", "ul li"):
        for el in soup.select(sel)[:40]:
            cands.append(el.get_text(" "))
    features = pick_features(cands, name)

    if not name:
        raise ScrapeError("상품명을 찾지 못했습니다.")

    def _dedupe(xs: List[str]) -> List[str]:
        out: List[str] = []
        for x in xs:
            if x not in out:
                out.append(x)
        return out

    return ProductInfo(
        name=name, price=price, price_text=price_text, features=features,
        image_urls=_dedupe(images)[:6], video_urls=_dedupe(videos)[:4],
        url=url, platform=platform, brand=brand,
    )


# ------------------------------------------------------------------ 진입점
def fetch_product(url: str, fetcher: Callable[[str], Tuple[str, str]] = fetch_html) -> ProductInfo:
    """URL -> ProductInfo. 차단·파싱 실패는 ScrapeError(ScrapeBlocked)로 올라간다."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ScrapeError(f"올바른 상품 주소가 아닙니다: {url[:60]}")
    final_url, html = fetcher(url)
    info = parse_product_html(html, final_url)
    info.url = url                       # 사용자가 준 주소를 기록(단축 링크 보존)
    info.platform = detect_platform(final_url) if detect_platform(final_url) != "generic" else detect_platform(url)
    return info


def product_from_manual(name: str, features_text: str = "", price: Any = None,
                        url: str = "", brand: str = "") -> ProductInfo:
    """크롤링이 막힐 때의 폴백: 상품명 + 핵심 특징(줄바꿈/쉼표 구분) 직접 입력."""
    name = _clean_text(name)
    if not name:
        raise ScrapeError("상품명을 입력해 주세요.")
    pieces = [p.strip(" -•·") for p in re.split(r"[\r\n]+|[;,，]|\s/\s", features_text or "")]
    feats: List[str] = []
    for p in pieces:
        p = _clean_text(p)
        if p and p not in feats:
            feats.append(p)
    p_int, p_txt = parse_price(price)
    return ProductInfo(name=name, price=p_int, price_text=p_txt, features=feats[:5],
                       url=url, platform="manual", brand=brand, manual=True)
