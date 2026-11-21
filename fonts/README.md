# fonts/

여기에 한국어 폰트 파일을 넣으면 상단 제목바·배지·자막 렌더에 **1순위로** 쓰입니다.

- 추천 폰트 6종 한 번에 받기: `python scripts/download_fonts.py` (주아·블랙한산스·도현·프리텐다드·노토산스·고운바탕, 모두 무료)
- 또는 `NanumGothicBold.ttf` 같은 파일을 직접 넣어도 됩니다
- 또는 시스템 폰트를 지정: `export SHORTS_FONT=/System/Library/Fonts/AppleSDGothicNeo.ttc`

macOS에는 `Apple SD Gothic Neo`가 기본으로 있어서 보통 이 폴더가 비어 있어도 한글이 잘 나옵니다.
폰트 파일은 `.gitignore`에 걸려 있어 Git에 올라가지 않습니다.
