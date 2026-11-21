"""영상 소스 자동 수집기.

상품명으로 한국어 + 영어/중국어 검색어를 만들고, yt-dlp로 공개 숏폼을 검색·다운로드한다.

  * 자동 검색 : 유튜브(쇼츠) · 빌리빌리(`bilisearch`) — yt-dlp가 검색을 지원하는 곳
  * 하이브리드: 샤오홍슈·도우인·틱톡은 검색이 막혀 있거나 불안정하므로
                검색어 + 검색 페이지 링크를 만들어 주고, 사용자가 찾은 URL이나 로컬 클립을
                extra_urls / local_paths 로 추가 투입한다(URL은 yt-dlp가 가능한 범위에서 받는다).
  * 받은 영상은 1080x1920(9:16)으로 맞추고, 원본 자막·워터마크 회피용으로
    기본 115% 중앙 크롭을 적용한 뒤 store 에 클립으로 등록한다(오디오는 버린다: 성우 음성을 쓰므로).

다운로더는 주입 가능하다(Downloader 프로토콜). 테스트는 가짜 구현을 넘긴다.
저작권: 받은 영상은 사용 권한이 있는 소스에만 쓰는 것을 전제로 한다.
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence
from urllib.parse import quote

from . import settings, store
from .render import RenderError, ffmpeg_exe, probe_media, _run
from .scraper import ProductInfo

CROP_MIN, CROP_MAX, CROP_DEFAULT = 100, 130, 115     # % (요청: 기본 112~118)


# ------------------------------------------------------------------ 키워드 (한 / 영 / 중)
# 쇼핑 상품에 자주 나오는 단어 사전. 사전에 없으면 (설치돼 있을 때만) 무료 번역기를 시도한다.
GLOSSARY: Dict[str, tuple] = {
    "정리함": ("organizer", "收纳盒"), "수납": ("storage", "收纳"), "접이식": ("foldable", "折叠"),
    "선풍기": ("fan", "风扇"), "핸디": ("handheld", "手持"), "무선": ("wireless", "无线"),
    "충전": ("rechargeable", "充电"), "믹서기": ("blender", "搅拌机"), "텀블러": ("tumbler", "保温杯"),
    "보온": ("insulated", "保温"), "청소기": ("vacuum cleaner", "吸尘器"), "청소": ("cleaning", "清洁"),
    "가습기": ("humidifier", "加湿器"), "제습기": ("dehumidifier", "除湿机"), "조명": ("lamp", "灯"),
    "무드등": ("night light", "氛围灯"), "램프": ("lamp", "台灯"), "이어폰": ("earbuds", "耳机"),
    "헤드폰": ("headphones", "头戴耳机"), "블루투스": ("bluetooth", "蓝牙"), "스피커": ("speaker", "音箱"),
    "마우스": ("mouse", "鼠标"), "키보드": ("keyboard", "键盘"), "거치대": ("stand holder", "支架"),
    "케이블": ("cable", "数据线"), "보조배터리": ("power bank", "充电宝"), "에어프라이어": ("air fryer", "空气炸锅"),
    "전기포트": ("electric kettle", "电热水壶"), "밥솥": ("rice cooker", "电饭煲"), "프라이팬": ("frying pan", "平底锅"),
    "냄비": ("pot", "锅"), "칼": ("knife", "刀"), "도마": ("cutting board", "砧板"), "수세미": ("scrubber", "洗碗刷"),
    "행주": ("dish cloth", "抹布"), "걸레": ("mop cloth", "拖布"), "물걸레": ("wet mop", "湿拖"),
    "빨래": ("laundry", "洗衣"), "건조대": ("drying rack", "晾衣架"), "옷걸이": ("hanger", "衣架"),
    "베개": ("pillow", "枕头"), "이불": ("blanket", "被子"), "매트": ("mat", "垫子"), "매트리스": ("mattress", "床垫"),
    "의자": ("chair", "椅子"), "책상": ("desk", "书桌"), "선반": ("shelf", "置物架"), "서랍": ("drawer", "抽屉"),
    "가방": ("bag", "包"), "백팩": ("backpack", "双肩包"), "지갑": ("wallet", "钱包"), "파우치": ("pouch", "收纳包"),
    "신발": ("shoes", "鞋"), "슬리퍼": ("slippers", "拖鞋"), "양말": ("socks", "袜子"), "모자": ("hat", "帽子"),
    "마스크": ("mask", "口罩"), "선크림": ("sunscreen", "防晒霜"), "샴푸": ("shampoo", "洗发水"),
    "칫솔": ("toothbrush", "牙刷"), "면도기": ("shaver", "剃须刀"), "드라이기": ("hair dryer", "吹风机"),
    "고데기": ("hair iron", "卷发棒"), "마사지": ("massage", "按摩"), "마사지기": ("massager", "按摩仪"),
    "요가": ("yoga", "瑜伽"), "운동": ("workout", "健身"), "덤벨": ("dumbbell", "哑铃"), "밴드": ("band", "弹力带"),
    "캠핑": ("camping", "露营"), "텐트": ("tent", "帐篷"), "랜턴": ("lantern", "提灯"), "의류": ("clothes", "衣服"),
    "장난감": ("toy", "玩具"), "강아지": ("dog", "狗狗"), "고양이": ("cat", "猫咪"), "반려동물": ("pet", "宠物"),
    "자동차": ("car", "汽车"), "차량용": ("car", "车载"), "방수": ("waterproof", "防水"), "대용량": ("large capacity", "大容量"),
    "휴대용": ("portable", "便携"), "미니": ("mini", "迷你"), "전동": ("electric", "电动"), "스마트": ("smart", "智能"),
    "자동": ("automatic", "自动"), "주방": ("kitchen", "厨房"), "욕실": ("bathroom", "浴室"), "다용도": ("multipurpose", "多功能"),
    "세트": ("set", "套装"), "필터": ("filter", "滤芯"), "정수기": ("water purifier", "净水器"), "물병": ("water bottle", "水壶"),
    "도시락": ("lunch box", "饭盒"), "반찬통": ("food container", "保鲜盒"), "텀블러": ("tumbler", "保温杯"),
    "커피": ("coffee", "咖啡"), "머그": ("mug", "马克杯"), "컵": ("cup", "杯子"), "손잡이": ("handle", "把手"),
}

_PROMO = re.compile(r"\d+\s*(?:개입|개|매|팩|세트|p|P|ea|EA|ml|ML|l|L|kg|g|cm|mm|입)\b|\d+\s*\+\s*\d+|"
                    r"무료배송|당일발송|특가|할인|정품|공식|국내|신상|인기|베스트|최신형|\[[^\]]*\]|\([^)]*\)|【[^】]*】")


def core_terms(product_name: str, brand: str = "", limit: int = 4) -> List[str]:
    """상품명에서 검색에 쓸 핵심 단어(최대 limit개)."""
    name = _PROMO.sub(" ", product_name or "")
    if brand:
        name = name.replace(brand, " ")
    toks = [t for t in re.split(r"[\s,/·+\-_]+", name) if t]
    toks = [t for t in toks if re.search(r"[가-힣A-Za-z]", t) and len(t) >= 2]
    # 사전에 있는 단어를 앞으로 -> 번역 가능한 단어가 먼저 잡힌다
    known = [t for t in toks if any(k in t for k in GLOSSARY)]
    rest = [t for t in toks if t not in known]
    out: List[str] = []
    for t in known + rest:
        if t not in out:
            out.append(t)
    return out[:limit]


def _glossary_lookup(token: str) -> Optional[tuple]:
    if token in GLOSSARY:
        return GLOSSARY[token]
    for k in sorted(GLOSSARY, key=len, reverse=True):     # "접이식정리함" 같은 붙여쓰기
        if k in token:
            return GLOSSARY[k]
    return None


def _default_translate(text: str, target: str) -> Optional[str]:
    """설치돼 있으면 deep-translator(무료·키 없음)로 번역. 실패하면 None."""
    try:
        from deep_translator import GoogleTranslator  # type: ignore
        return GoogleTranslator(source="ko", target=target).translate(text) or None
    except Exception:
        return None


def build_keywords(product: ProductInfo,
                   translate: Optional[Callable[[str, str], Optional[str]]] = None) -> Dict[str, Any]:
    """{'terms': [...], 'ko': [...], 'en': [...], 'zh': [...], 'warnings': [...]}"""
    translate = translate or _default_translate
    terms = core_terms(product.name, product.brand)
    warnings: List[str] = []
    ko_core = " ".join(terms[:3]) or product.name[:20]

    en_words: List[str] = []
    zh_words: List[str] = []
    for t in terms:
        hit = _glossary_lookup(t)
        if hit:
            en_words.append(hit[0]); zh_words.append(hit[1])
            continue
        en = translate(t, "en")
        zh = translate(t, "zh-CN")
        if en:
            en_words.append(en)
        if zh:
            zh_words.append(zh)
    en_core = " ".join(dict.fromkeys(en_words))
    zh_core = "".join(dict.fromkeys(zh_words))
    if not en_core:
        warnings.append("영어 키워드를 만들지 못했어요(사전에 없는 단어). 한국어 검색만 사용합니다.")
    if not zh_core:
        warnings.append("중국어 키워드를 만들지 못했어요. 샤오홍슈/도우인은 직접 URL을 넣어 주세요.")

    return {
        "terms": terms,
        "ko": [f"{ko_core} 추천 shorts", f"{ko_core} 리뷰 shorts"],
        "en": [f"{en_core} review shorts", f"{en_core} unboxing shorts"] if en_core else [],
        "zh": [f"{zh_core} 开箱", f"{zh_core} 好物推荐"] if zh_core else [],
        "warnings": warnings,
    }


def manual_search_links(kw: Dict[str, Any]) -> Dict[str, str]:
    """사용자가 직접 열어 URL을 고를 수 있는 검색 페이지(샤오홍슈/도우인/틱톡/유튜브)."""
    zh = (kw.get("zh") or [""])[0].replace(" 开箱", "").strip()
    en = (kw.get("en") or [""])[0].replace(" review shorts", "").strip()
    ko = (kw.get("ko") or [""])[0].replace(" 추천 shorts", "").strip()
    links: Dict[str, str] = {}
    if zh:
        links["샤오홍슈"] = f"https://www.xiaohongshu.com/search_result?keyword={quote(zh)}"
        links["도우인"] = f"https://www.douyin.com/search/{quote(zh)}"
    if en or ko:
        links["틱톡"] = f"https://www.tiktok.com/search?q={quote(en or ko)}"
    if ko:
        links["유튜브 쇼츠"] = f"https://www.youtube.com/results?search_query={quote(ko + ' shorts')}"
    return links


# ------------------------------------------------------------------ 다운로더
class Downloader(Protocol):
    def search(self, query: str, n: int, site: str = "youtube") -> List[Dict[str, Any]]:
        """[{'url','title','duration','id'}]"""

    def download(self, url: str, out_dir: Path) -> Path:
        """영상 파일 하나를 받아 경로를 돌려준다."""


class YtDlpDownloader:
    """yt-dlp 파이썬 API 래퍼(지연 import: 설치 안 돼 있으면 안내 메시지)."""

    MAX_SEC, MIN_SEC = 90, 3

    def __init__(self, cookies_from_browser: Optional[str] = None, max_mb: int = 200) -> None:
        self.cookies_from_browser = cookies_from_browser
        self.max_bytes = max_mb * 1024 * 1024

    def _lib(self) -> Any:
        try:
            import yt_dlp  # type: ignore
            return yt_dlp
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("yt-dlp 가 설치되어 있지 않습니다. 터미널: pip install yt-dlp") from exc

    def _base_opts(self) -> Dict[str, Any]:
        o: Dict[str, Any] = {"quiet": True, "no_warnings": True, "noplaylist": True,
                             "socket_timeout": 15, "retries": 2, "nocheckcertificate": False}
        if self.cookies_from_browser:
            o["cookiesfrombrowser"] = (self.cookies_from_browser,)
        return o

    def search(self, query: str, n: int, site: str = "youtube") -> List[Dict[str, Any]]:
        yt = self._lib()
        prefix = {"youtube": "ytsearch", "bilibili": "bilisearch"}.get(site)
        if not prefix:
            raise RuntimeError(f"{site} 검색은 지원하지 않습니다(URL을 직접 넣어 주세요).")
        opts = self._base_opts() | {"extract_flat": "in_playlist", "skip_download": True}
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{int(n)}:{query}", download=False) or {}
        out: List[Dict[str, Any]] = []
        for e in info.get("entries") or []:
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url")
            if not url:
                continue
            dur = e.get("duration")
            if dur is not None and not (self.MIN_SEC <= float(dur) <= self.MAX_SEC):
                continue
            out.append({"url": url, "title": e.get("title") or "", "duration": dur, "id": e.get("id") or url})
        return out

    def download(self, url: str, out_dir: Path) -> Path:
        yt = self._lib()
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = self._base_opts() | {
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "format": "bv*[height<=1920]/b",          # 영상만(오디오는 쓰지 않는다)
            "merge_output_format": "mp4",
            "max_filesize": self.max_bytes,
            "match_filter": yt.utils.match_filter_func(f"duration<={self.MAX_SEC} & duration>={self.MIN_SEC}"),
        }
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("조건(길이/크기)에 맞지 않아 건너뜁니다.")
            if info.get("entries"):
                info = info["entries"][0]
            path = None
            for d in info.get("requested_downloads") or []:
                path = d.get("filepath") or path
            path = path or ydl.prepare_filename(info)
        p = Path(path)
        if not p.exists():
            raise RuntimeError("다운로드 파일을 찾지 못했습니다.")
        return p


# ------------------------------------------------------------------ 9:16 정규화
def clamp_crop_pct(v: Any) -> int:
    try:
        return int(max(CROP_MIN, min(CROP_MAX, round(float(v)))))
    except Exception:
        return CROP_DEFAULT


def normalize_to_vertical(src: Path, dst: Path, crop_pct: Any = CROP_DEFAULT,
                          out_w: int = settings.OUT_W, out_h: int = settings.OUT_H,
                          fps: int = settings.OUT_FPS, max_seconds: float = 60.0) -> Dict[str, Any]:
    """9:16 꽉 채우기(cover) + crop_pct% 중앙 확대 크롭. 오디오 제거.

    crop_pct=115 -> 커버 크기의 1.15배로 키운 뒤 가운데 1080x1920만 남긴다
    (가장자리 워터마크·원본 자막 띠가 잘려 나간다). 100이면 확대 없이 커버만.
    """
    pct = clamp_crop_pct(crop_pct)
    z = pct / 100.0
    big_w = int(round(out_w * z / 2.0)) * 2
    big_h = int(round(out_h * z / 2.0)) * 2
    vf = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={out_w}:{out_h},setsar=1,fps={fps},format=yuv420p")
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-loglevel", "error", "-i", str(src),
           "-t", f"{max_seconds:.2f}", "-an", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(dst)]
    cp = _run(cmd, timeout=600)
    if cp.returncode != 0 or not dst.exists():
        raise RenderError("영상 리사이즈 실패: " + (cp.stderr or "").strip()[-300:])
    info = probe_media(dst)
    return {"duration": info["duration"], "width": info["width"], "height": info["height"], "crop_pct": pct}


def register_clip(path: Path, name: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """CLIP_DIR 안의 mp4를 store 에 클립으로 등록(렌더러가 clip_id 로 찾는다)."""
    info = probe_media(path)
    meta = {
        "id": path.stem, "name": name, "path": str(path), "size": path.stat().st_size,
        "duration": info["duration"], "width": info["width"], "height": info["height"],
        "has_audio": info["has_audio"], "created_at": time.time(), "origin": "auto",
    }
    if extra:
        meta.update(extra)
    return store.save_clip_meta(meta)


# ------------------------------------------------------------------ 수집 오케스트레이션
@dataclass
class SourceResult:
    clips: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    keywords: Dict[str, Any] = field(default_factory=dict)
    search_links: Dict[str, str] = field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        return round(sum(float(c.get("duration") or 0) for c in self.clips), 2)


def _ingest(src: Path, label: str, crop_pct: Any, origin: str, result: SourceResult) -> bool:
    cid = store.new_id()
    dst = settings.CLIP_DIR / f"{cid}.mp4"
    try:
        info = normalize_to_vertical(src, dst, crop_pct)
        if info["duration"] < 1.6:
            dst.unlink(missing_ok=True)
            result.warnings.append(f"너무 짧아 건너뜀: {label}")
            return False
        meta = register_clip(dst, label[:60], {"origin": origin, "crop_pct": info["crop_pct"]})
        result.clips.append(meta)
        return True
    except (RenderError, OSError) as exc:
        dst.unlink(missing_ok=True)
        result.warnings.append(f"변환 실패({label}): {exc}")
        return False


def collect_sources(product: ProductInfo, *,
                    downloader: Optional[Downloader] = None,
                    want_seconds: float = 40.0, max_clips: int = 8,
                    extra_urls: Sequence[str] = (), local_paths: Sequence[str] = (),
                    crop_pct: Any = CROP_DEFAULT, auto_search: bool = True,
                    translate: Optional[Callable[[str, str], Optional[str]]] = None,
                    per_query: int = 6,
                    progress: Optional[Callable[[str], None]] = None) -> SourceResult:
    """사용자 투입(로컬/URL)을 먼저 쓰고, 부족하면 자동 검색으로 채운다.

    want_seconds: 이만큼 모이면 멈춘다(컷마다 다른 구간을 쓰므로 넉넉히 받는다).
    """
    say = progress or (lambda _m: None)
    result = SourceResult()
    kw = build_keywords(product, translate)
    result.keywords = kw
    result.warnings += kw["warnings"]
    result.search_links = manual_search_links(kw)
    tmp = settings.TMP_DIR / f"dl_{store.new_id()}"
    tmp.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    dl = downloader

    def full() -> bool:
        return result.total_seconds >= want_seconds or len(result.clips) >= max_clips

    try:
        # 1) 사용자가 준 로컬 클립
        for lp in local_paths:
            p = Path(lp).expanduser()
            if not p.exists():
                result.warnings.append(f"로컬 파일이 없습니다: {lp}")
                continue
            say(f"로컬 클립 변환: {p.name}")
            _ingest(p, p.name, crop_pct, "local", result)

        # 2) 사용자가 준 URL + 상품 페이지의 상세 영상
        direct = [(u, "url") for u in extra_urls] + [(u, "product") for u in product.video_urls]
        for url, origin in direct:
            if full() or url in seen:
                continue
            seen.add(url)
            if dl is None:
                dl = YtDlpDownloader()
            say(f"URL 다운로드: {url[:60]}")
            try:
                f = dl.download(url, tmp)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"다운로드 실패({url[:50]}): {exc}")
                continue
            _ingest(f, Path(f).stem, crop_pct, origin, result)

        # 3) 자동 검색 (유튜브 쇼츠 → 빌리빌리)
        if auto_search and not full():
            dl = dl or YtDlpDownloader()
            plan: List[tuple] = [("youtube", q) for q in kw["ko"] + kw["en"]] + \
                                [("bilibili", q) for q in kw["zh"][:1]]
            for site, query in plan:
                if full():
                    break
                say(f"검색({site}): {query}")
                try:
                    hits = dl.search(query, per_query, site)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"검색 실패({site}/{query}): {exc}")
                    continue
                for h in hits:
                    if full():
                        break
                    key = str(h.get("id") or h.get("url"))
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        f = dl.download(h["url"], tmp)
                    except Exception as exc:  # noqa: BLE001
                        result.warnings.append(f"건너뜀({(h.get('title') or h['url'])[:30]}): {str(exc)[:80]}")
                        continue
                    _ingest(f, h.get("title") or Path(f).stem, crop_pct, site, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not result.clips:
        result.warnings.append("사용할 영상 소스를 하나도 모으지 못했습니다. URL이나 로컬 클립을 추가해 주세요.")
    return result
