# Devin Gauntlet Runner — 실행 코드 v0.1.0

**문서/skill을 AI가 읽고 스스로 반복하기를 기대하는 패키지가 아닙니다.**
Python 프로세스가 lead 호출, 작업별 builder/critic 호출, 실패 후 재호출,
의존 관계, 통합, 최종 재평가, 중단 및 재개 상태를 관리합니다.

이 코드는 이 대화의 요청을 위해 작성한 **독립 대체 구현**입니다.
Matt Shumer나 Cognition의 공식 배포본이 아니며, `kamtS/gauntlet-loop`의
명령행·설정 파일과 호환되는 포크라고 주장하지 않습니다.

## 실행 검증 상태

- 로컬 Python/Git 및 실제 subprocess를 사용한 오프라인 테스트를 제공합니다.
- 테스트의 모델 역할은 `examples/fake_devin.py`가 수행합니다. 이는 **Devin도 AI도 아닙니다**.
- 이 제작 환경에는 Devin CLI가 설치되어 있지 않아 실제 인증, 모델 호출,
  Devin 권한 집행, 이미지 관찰 도구, 요금 소비를 검증하지 않았습니다.
- 실제 호출 코드는 존재하며, 기본 백엔드는 `devin`입니다. `--print`,
  `--prompt-file`, `--config`, `--respect-workspace-trust`, `--sandbox`,
  `--permission-mode`를 사용합니다. 근거는 `SOURCES.md`에 있습니다.

## 단일 파일 실행

소스 폴더 없이 배포된 `devin-gauntlet.pyz` 하나로 같은 명령을 실행할 수 있습니다.
이는 Python zipapp이며 네이티브 바이너리/.exe가 아닙니다. Python 3.11 이상은 필요합니다.

```bash
python3 devin-gauntlet.pyz --help
python3 devin-gauntlet.pyz doctor
```

아래의 `python3 -m devin_gauntlet` 대신 `python3 devin-gauntlet.pyz`를 쓰면 됩니다.

## Quickstart

```bash
# 설치 (택 1)
pipx install git+https://github.com/eclipse1228/gauntlet-loop   # devin-gauntlet CLI
# 또는 Releases의 단일 파일 zipapp:
curl -LO https://github.com/eclipse1228/gauntlet-loop/releases/latest/download/devin-gauntlet.pyz

# 깨끗한 git 저장소를 대상으로:
devin-gauntlet init my-run --project /path/to/repo --goal "목표" --reference ref.png
devin-gauntlet run my-run --allow-execution

# 오프라인 데모 (Devin 로그인 불필요 — fake agent로 전체 루프 재현):
python3 examples/offline_demo.py
```

## 1. 요구 환경

Python 3.11 이상, Git, 설치·인증된 Devin CLI가 필요합니다.
Python 패키지 의존성은 없습니다. Linux/WSL2를 대상으로 테스트했으며,
macOS는 POSIX 구현 경로가 있지만 실제 실행 검증은 하지 않았습니다.
**네이티브 Windows 프로세스 감독은 구현하지 않았습니다. WSL2 안에서 실행하십시오.**

Devin 설치·인증 및 sandbox 사전 확인:

```bash
devin --version
devin auth login
devin auth status
devin sandbox setup
```

Devin 공식 문서상 Linux/WSL의 sandbox에는 `bwrap`과 `socat`이 필요합니다.
이 패키지는 sandbox 실패 시 자동으로 위험한 우회 모드로 전환하지 않습니다.

압축을 푼 폴더에서 다음 명령으로 시작할 수 있습니다. pip 설치는 필수가 아닙니다.

```bash
cd devin-gauntlet
python3 -m devin_gauntlet doctor
python3 -m devin_gauntlet --help
```

## 2. 실제 Devin으로 실행

대상은 **커밋이 존재하고 변경 사항이 없는 Git 저장소**여야 합니다.
원본의 무시된 파일, 설치된 `node_modules`, 비밀 환경 파일을 자동 복사하지 않습니다.
작업용 디렉터리는 대상 저장소 **밖**에 둡니다.

아래의 경로와 목표 문자열을 자신의 작업으로 바꿉니다. 이미지 레퍼런스가 아닌
작업은 `--reference`를 생략할 수 있으며, lead가 실제 비교물 또는 측정 기준을 정합니다.

```bash
python3 -m devin_gauntlet init /absolute/path/my-gauntlet-run \
  --project /absolute/path/my-project \
  --goal '달성하려는 실제 목표' \
  --reference /absolute/path/reference.png \
  --parallel 2

python3 -m devin_gauntlet run /absolute/path/my-gauntlet-run \
  --allow-execution
```

`--parallel`은 동시 실행 프로세스 상한입니다. **작업 개수나 반복 횟수가 아닙니다.**
작업의 개수·분할·의존 관계는 lead가 결정합니다. 모델을 고정하려면 init에
`--model`을 지정하거나 run 디렉터리의 `config.json`에서 역할별 `models`를 설정합니다.
특정 모델이 Claude Code의 `ultracode`와 동등하다고 가정하지 않습니다.

### 실행 승인 범위

`--allow-execution`은 다음 작업에 대한 명시적 승인을 뜻합니다.

1. Devin이 복제된 작업 공간에서 소스 코드를 수정하고 명령을 실행합니다.
2. 비대화형으로 만든 작업 디렉터리에 대해 `--respect-workspace-trust false`를 사용합니다.
3. Lead가 작성한 `prepare`, `capture`, `checks`의 명령을 실행합니다.

**3번의 명령은 Python이 직접 실행하는 로컬 subprocess이며 Devin sandbox 안에서
실행되는 것이 아닙니다.** 소스 코드·설치 스크립트·테스트는 호스트 사용자 권한과
환경 변수에 접근할 수 있습니다. 따라서 신뢰하는 프로젝트와 별도의 개발용 환경에서
실행해야 합니다. 이 패키지는 호스트 전체를 격리하는 컨테이너/VM 제품이 아닙니다.
Git 원본을 직접 수정하지 않는다는 것은 호스트 파일 전체에 대한 보안 보증이 아닙니다.

먼저 계획과 실행 명령을 확인하는 경로도 있습니다. 작업 분할을 사람이 작성할 필요는 없습니다.

```bash
python3 -m devin_gauntlet plan /absolute/path/my-gauntlet-run --allow-execution
cat /absolute/path/my-gauntlet-run/plan.json
python3 -m devin_gauntlet run /absolute/path/my-gauntlet-run --allow-execution
```

`plan`도 lead를 실제 실행하므로 인증 및 실행 승인이 필요합니다.
계획이 고정된 뒤 목표·평가 규칙·계획·레퍼런스 바이트를 바꾸면 실행을 차단합니다.
다른 기준으로 실행하려면 새 run을 만듭니다. 모델, 동시성, 호출 timeout은 운영 설정입니다.

## 3. 코드가 진행하는 순서

```text
Python supervisor
  └─ Devin lead → 실제 작업 목록 + 의존 관계 + 기준 + 산출물 수집 명령
       └─ 작업별 독립 Git checkout
            ├─ Devin builder (새 CLI 프로세스)
            ├─ Python: 소스 커밋
            ├─ Python: 해당 커밋의 별도 checkout에서 준비·캡처·검증 명령 실행
            ├─ Python: 실제 산출물로 critic packet 생성
            └─ Devin critic (새 CLI 프로세스, builder 응답 미전달)
                 ├─ 실제 기준 미달 → 차이를 해당 builder에 전달 → 다음 회차
                 └─ 통과 → 다른 작업의 변경과 충돌하지 않는 상태로 통합

모든 작업 통합 → 선택적 smoothing → 동일 통합 커밋에서 기존 모든 기준 재평가
  ├─ 어느 항목이 다시 실패 → 그 항목의 루프 재개
  └─ 모두 통과 → BAR_MET + result.patch
```

Devin 내부 `/loop`나 native subagent 스케줄링에 다음 회차를 맡기지 않습니다.
외부 Python이 별개의 Devin CLI invocation을 생성합니다. `--resume`/`--continue`를
사용하지 않습니다. Builder의 stdout이 `PASS`, `완료`, `중단하라`여도 반복 제어에는 쓰지 않습니다.

`engine.py`의 `build_until_pass()`에는 **품질 반복의 `max_passes`가 없습니다.**
코드의 `while True`가 검증 실패 후 다음 회차를 생성합니다.
호출 오류·잘못된 출력은 성공이 아니라 `BLOCKED`이며, 오류를 해결한 뒤 명시적으로 재개할 수 있습니다.
프로세스 종료·정전·인증 실패까지 없애는 무오류/영구 실행을 보증하지는 않습니다.

### A/B 비교

실제 candidate/reference 파일을 회차마다 `A`, `B`에 무작위 배정하고 파일명을 정규화합니다.
매핑, builder 보고서, 이전 대화, run 로그는 critic payload에 넣지 않습니다.
이 구현에서 동률은 승리가 아니므로 계속 개선합니다.

블라인드는 **경로·라벨·전달 데이터 차원**의 처리입니다. 로고, 워터마크, EXIF,
문서 내용 등 산출물 자체에 들어 있는 정체 정보를 제거하거나 의미적 익명성을 보증하지 않습니다.

### 측정 기준

측정 모드는 실행 가능한 `checks`를 요구합니다. 각 검증 명령의 실제 종료 코드와
stdout/stderr를 수집하고 critic에게 제공합니다. 검증 명령 중 하나라도 실패하면
critic이 `PASS`라고 써도 통과시키지 않습니다. 테스트 자체가 충분하거나 올바른지까지
기계적으로 증명하는 것은 아닙니다. Lead가 정한 기준과 검증 명령은 `plan.json`에 공개됩니다.

### 판정 형식

Lead와 critic의 응답에는 호출마다 새로운 nonce가 붙은 JSON envelope를 요구합니다.
Critic은 해당 packet ID와 모든 실제 파일에 대한 observations를 반환해야 합니다.
단순 `PASS` 문자열, 오래된 packet ID, 존재하지 않는 파일 인용, 누락된 관찰은 거부합니다.

**이 검사는 모델이 픽셀을 실제로 이해했음을 증명하지 않습니다.**
형식적으로 올바른 관찰을 꾸며낼 가능성을 없애는 시스템은 아닙니다. 이미지나 파일을
읽지 못한 critic에는 `UNJUDGEABLE`을 요구하고, 이를 성공으로 간주하지 않습니다.
실제 이미지 관찰 능력은 설치된 Devin/모델 환경에서 확인해야 합니다.

## 4. 중단, 재개, 상태

실행 중인 터미널에서 Ctrl+C를 누르거나, 다른 터미널에서 다음 명령을 실행합니다.

```bash
python3 -m devin_gauntlet stop /absolute/path/my-gauntlet-run
python3 -m devin_gauntlet status /absolute/path/my-gauntlet-run
python3 -m devin_gauntlet resume /absolute/path/my-gauntlet-run --allow-execution
```

`stop`은 요청 파일을 만들며, 감독 프로세스가 이를 확인하여 관리 중인 프로세스
그룹에 TERM/KILL을 보냅니다. `USER_STOPPED` 상태로 바뀌었는지 확인합니다.
일반적인 자식 프로세스는 종료되지만, 스스로 다른 process group/session으로 이탈한
프로세스나 외부 cloud 작업의 중단은 보증하지 않습니다. 그런 작업의 생성을 역할 입력에서 금지합니다.

SIGKILL·정전으로 감독 프로세스가 사라지는 경우에는 즉시 정리할 수 없습니다.
재개 시 기록된 PID가 살아 있으면 자동으로 새 writer를 실행하지 않고 차단합니다.
PID 재사용 가능성 때문에 기록된 PID를 무조건 죽이지 않습니다. 사용자가 해당 프로세스를
확인·종료한 후 재개해야 합니다. Git 복제/병합 등 짧은 동기 작업 중에는 중단 반영이 지연될 수 있습니다.

사용자가 정한 시간 한도를 추가할 수 있습니다. 기본값은 없습니다.

```bash
python3 -m devin_gauntlet run /absolute/path/my-gauntlet-run \
  --allow-execution --max-seconds 3600
```

이는 해당 호출의 **사용자 지정 실행 한도**이며, Matt 원문의 고정 반복 횟수나
품질 판정 규칙이 아닙니다. 실제 토큰/금액 한도 집계는 구현하지 않았습니다.

| 상태 | 의미 | 종료 코드 |
|---|---|---:|
| `BAR_MET` | 동일 통합 커밋이 설정된 모든 판정을 통과 | 0 |
| `PLAN_READY` | 계획과 reference가 고정됨; 제품 완성이 아님 | 0 |
| `USER_STOPPED` | 사용자 중단; 통과를 의미하지 않음 | 130 |
| `USER_LIMIT_REACHED` | 사용자 지정 실행 시간 한도 | 3 |
| `BLOCKED` | 호출·권한·출력·데이터 무결성 등 운영 오류 | 2 |

## 5. 실제 결과 및 진행 화면

```text
my-gauntlet-run/
├── config.json               운영 설정, 목표
├── plan.json                 Lead가 작성한 고정 실행 계획
├── state.json                원자적으로 저장되는 진행 상태
├── STOP                      사용자 중단 요청 (있을 때)
├── runner.lock               중복 감독 프로세스 방지
├── references/               입력 및 해시가 고정된 reference
├── work/
│   ├── integration/          실제 통합 결과 Git checkout
│   ├── builders/<task-id>/   작업별 결과; 중단·재개 시 유지
│   └── lead/                 Lead 작업 공간
├── private/
│   ├── calls/                실제 호출 argv, stdout/stderr, 반환 코드
│   ├── evaluations/          A/B 매핑, packet, 해시, 판정
│   ├── active.json           관리 중인 프로세스
│   └── events.jsonl          반복/프로세스/판정/통합 이벤트
├── public/
│   ├── index.html            자동 갱신 진행 화면
│   └── evidence/             회차별 실제 산출물/검증 로그 사본
└── result.patch              BAR_MET의 통합 결과 패치
```

진행 페이지의 제목은 lead가 지정하고, 표·증거 링크 갱신은 Python이 수행합니다.
이는 모델이 진행 문서를 갱신하지 않더라도 상태가 기록되게 하는 구현입니다.
회차별 실제 이미지나 파일은 evidence 링크에 남습니다.

```bash
python3 -m http.server 8000 \
  --bind 127.0.0.1 \
  --directory /absolute/path/my-gauntlet-run/public
```

브라우저에서 `http://127.0.0.1:8000`을 엽니다. private 디렉터리를 서비스하지 마십시오.
보관되는 실제 산출물·로그는 계속 증가하며 자동 삭제/보관 기간 정책은 없습니다.

원본에 적용할 때에는 사용자가 직접 다음과 같이 확인 후 적용합니다.

```bash
cd /absolute/path/my-project
git apply --check /absolute/path/my-gauntlet-run/result.patch
git apply /absolute/path/my-gauntlet-run/result.patch
```

중단한 run의 현재 **통합 완료된 부분만** 패치로 내보내려면:

```bash
python3 -m devin_gauntlet export /absolute/path/my-gauntlet-run
```

미통과 builder의 미통합 변경은 그 builder checkout에 보존되며 이 패치에는 포함되지 않습니다.

## 6. 오프라인 실행 확인

실제 모델 호출 전에 Python 루프 자체를 실행해 볼 수 있습니다.

```bash
python3 examples/offline_demo.py /tmp/gauntlet-demo
python3 -m unittest discover -s tests -v
```

데모는 실제 정수 파일을 0에서 시작해 6인 reference보다 커질 때까지 바꿉니다.
모의 builder는 매번 `PASS, stop now`를 출력하지만, 실행기는 이를 무시합니다.
7회 build, 7회 critic, 같은 통합 커밋의 최종 critic 후 종료됩니다.
이 데모는 워크플로의 실행 시험이지 실제 AI 성능 시험이 아닙니다.

테스트에는 의존 관계, 병렬 builder, 최종 통합 시 회귀 발견과 루프 재개,
reference/plan 변경 차단, 잘못된 판정 차단, 실패한 실제 check에 대한 거짓 PASS 무시,
프로세스 그룹 종료, 중복 실행 방지, 중단 후 재개가 포함됩니다.

## 7. 구현 경계

Critic은 임시 evidence 전용 디렉터리에서 실행합니다. 알려진 원본/worker/run-log/reference
경로의 읽기를 거부하고 edit/exec/MCP 및 쓰기를 제한하는 Devin 설정을 전달합니다.
그러나 이 설정의 실제 집행은 **현재 설치된 Devin CLI**에 의존합니다. 사용자 전역 규칙,
enterprise 정책, CLI가 자동으로 불러오는 항상-on context를 완전히 제거했다고 주장하지 않습니다.
Critic 프로세스 전체를 별도 OS 사용자/컨테이너에 넣는 구현은 포함하지 않았습니다.

일반 Git 저장소를 대상으로 합니다. Submodule/LFS 자동 준비, 서비스별 로그인,
데이터베이스/클라우드 배포, 브라우저 캡처 서버 수명 관리, API 요금 계량은 별도 구현이 필요합니다.
`prepare/capture/checks`가 실제 프로젝트에 맞는지는 생성된 계획과 실행 로그로 확인해야 합니다.

출처 요구와 코드의 상태명·파일 형식·Git 통합 방식은 구분합니다.
Matt 원문은 Python, JSON nonce, Git checkout, fast-forward merge를 규정하지 않았습니다.
이 파일 형식과 프로세스 감독 방식은 실행 가능한 코드로 옮기기 위해 이 구현에 사용된 것입니다.
