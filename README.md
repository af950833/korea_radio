# Korea Radio for Home Assistant

![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.6+-blue.svg)
![Version](https://img.shields.io/github/v/release/<your_repo>/korea_radio)
![License](https://img.shields.io/badge/license-MIT-green)

한국 주요 라디오 채널을 Home Assistant에서 재생할 수 있는 커스텀 컴포넌트입니다.
Google Home(Chromecast) 등 `media_player` 엔티티로 바로 캐스트하여 사용할 수 있습니다.

---

## 🎬 미리보기

### 📻 재생 화면

![preview](https://raw.githubusercontent.com/af950833/korea_radio/main/images/preview.png)

### 🎚 채널 선택

![preview2](https://raw.githubusercontent.com/af950833/korea_radio/main/images/preview2.png)

---

## ✨ 주요 기능

* 📻 국내 라디오 채널 지원 (KBS / MBC / SBS / 기타)
* 🔄 방송사 API 기반 실시간 스트림 자동 획득
* 🎧 ffmpeg 변환 스트리밍 지원
* 🎚 **음질 선택 (128 ~ 320 kbps)**
* 📡 Google Home / Chromecast 캐스트 지원
* 🎚 볼륨 슬라이더 지원
* 🖼 채널별 아이콘 표시
* 🌐 내부 IP 자동 탐지 (설정 불필요)
* 🧩 HACS 설치 지원
* TTS 로 인해 방송 정지된 경우에도 자동 재시작

---

## 📻 지원 채널

### 🟦 KBS

* KBS 1Radio
* KBS 3Radio
* KBS Classic FM
* KBS Cool FM
* KBS Happy FM

### 🟥 MBC

* MBC FM
* MBC FM4U
* MBC Mini AllThatMusic

### 🟩 SBS

* SBS Power FM
* SBS Love FM
* SBS GolillaM

### 🟨 기타

* TBS FM
* TBS eFM
* TBN FM
* IFM
* EBS FM
* CBS FM
* CBS JOY4U
* CBS Music FM
* YTN News
* OBS FM
* AFN FM Humphreys

---

## 🎧 음질 설정 (Bitrate)

스트림은 ffmpeg를 통해 변환되며, 음질을 선택할 수 있습니다.

### 📊 옵션

* 128 kbps
* 192 kbps (기본)
* 256 kbps
* 320 kbps

### ⚖️ 추천

| 환경     | 추천           |
| ------ | ------------ |
| 일반 사용  | 192 kbps     |
| 고음질    | 256~320 kbps |
| 저사양 서버 | 128 kbps     |

### ⚠️ 참고

* 비트레이트 ↑ → CPU 사용량 ↑
* 원본 음질 이상으로 향상되지는 않음

---

## 📦 설치 (HACS)

1. HACS → Integrations → ⋮ → Custom repositories
2. Repository 추가

```
https://github.com/af950833/korea_radio
```

3. Category: **Integration**
4. 설치 후 재시작

---

## ⚙️ 설정

* 설정 → 기기 및 서비스 → 통합 추가
* "Korea Radio" 선택

설정 항목:

* 사용할 `media_player` 선택
* 비트레이트 선택

---

## 🎮 사용 방법

### UI

* 미디어 플레이어 카드에서 채널 선택

### 서비스 호출

```yaml
service: media_player.select_source
target:
  entity_id: media_player.korea_radio
data:
  source: YTN News
```

---

## 🖼 아이콘

```text
custom_components/korea_radio/icons/
```

예:

```text
kbs_cool.jpg
sbs_power.jpg
mbc_fm.jpg
tbnfm.jpg
```

---

## ⚠️ 주의

* HA와 Google Home은 같은 네트워크(LAN)
* HA는 http://192.x.x.x:8123 과 같이 내부망에서 사설IP 접속이 가능해야 됨
* Docker → `network_mode: host` 권장
* 일부 방송사는 스트림 변경 가능

---

## 🛠 문제 해결

### 캐스트 실패

* 내부 IP 확인
* 동일 네트워크 확인

### 채널 재생 안됨

* Issue 등록

---

### 버전 기록

* 2026/03/24 Ver 1.0.0 - Initial Release
* 2026/03/25 Ver 1.0.5 - TTS 로 인해 중단된 방송을 자동으로 재시작 기능 추가
* 2026/03/25 Ver 1.0.7 - 전원 버튼 동작 개선, 버그 픽스 및 코드 최적화
* 2026/03/26 Ver 1.0.8 - OBS 추가 및 버그 수정
* 2026/03/26 Ver 1.0.9 - 채널 선택 추가 및 메뉴 번역 추가
* 2026/03/26 Ver 1.1.0 - SBS 고릴라M 채널 추가
* 2026/03/27 Ver 1.1.1 - MBC Mini AllThatMusic, CBS JOY4U, AFN FM Humphreys 채널 추가

---
