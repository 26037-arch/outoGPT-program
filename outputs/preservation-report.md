# 전체 대화 보존 개선 및 검증 결과

기준 커밋: `8915ba3060f84ced172bf99cb4f646018e137ac9`.
원격 HEAD도 같은 커밋임을 확인했다. 변경은 로컬 작업 트리에 적용했다.

## 진단 자료 및 실제 검증 범위

요청한 `outputs/findings.md`, `outputs/comparison.json`, `outputs/validation.json`은
작업 시작 시 저장소에 없었고 사용자 홈 검색에서도 찾지 못했다. 읽었다고
간주하거나 이 파일들을 재작성하지 않았다. 사용자가 명시한 `cursor: null`,
`page_info`, 숨겨진 system cursor 등의 진단 사실을 완료 조건에 반영했다.

기존 BrowserSession과 전용 Chrome 프로필로 **실제 CDP 연결은 성공**했다.
그러나 설정된 프로젝트를 열면 Google 로그인 화면으로 이동해 프로젝트 및
대화 응답을 받지 못했다. **실제 계정의 전체 보존 성공은 검증하지 못했다.**
실제 사용자 채팅의 MD를 생성하거나 변경하지 않았다. 인증 우회나 setup 변경은 없다.
별도 기록: [live-schema-probe.json](live-schema-probe.json).

## 변경한 완료 조건

- 페이지 이동 전에 CDP의 요청·응답·완료·실패 감시를 등록한다. 이벤트 콜백에서는
  작업을 큐에 넣고, 본문 수집·JSON 파싱·스키마 검증이 끝나야 pending에서 제거한다.
- 프로젝트는 실제 요청의 cursor와 응답의 cursor를 연결하며, 최초 요청부터
  `cursor: null`까지 이어져야 완료한다. 채팅 ID는 프로젝트 전용 응답에서 수집한다.
  목록의 정지, 빈 화면, 스크롤 끝은 완료 근거로 사용하지 않는다.
- 개별 대화는 최초 요청과 `messages?before=…`를 추적한다.
  `page_info.start_cursor`와 다음 요청의 before가 연결되고,
  `has_previous_page === false`가 확인되어야 한다. 진행 중 요청이나 미처리 본문이
  있으면 완료하지 않는다. 순서가 뒤바뀐 응답도 연결하고, 단절·순환·충돌은 거부한다.
- DOM의 `data-message-id`를 네트워크 메시지 ID와 연결한다. start_cursor를 DOM의
  첫 메시지와 비교하지 않는다. 겹치지 않는 가상화 화면에서도 이미 수집한 UUID를
  삭제하지 않는다. 메시지 순서는 모든 페이지의 parent 연결로 검증한다.
- 모든 메시지의 역할과 내용을 확인한다. Markdown 서식·공백을 정규화한 텍스트를
  비교하며, 정확한 네트워크 원문도 같은 MD에 보존한다. 파일명·이미지 참조는 DOM
  첨부 증거와 대조한다. system/tool/developer 및 명시적으로 숨겨진 메시지는
  별도 원문 증거로 보존한다. 알 수 없는 스키마·콘텐츠 형식·분기는 완료하지 않는다.
- 생성 중에는 기다린다. 생성 종료 후 같은 채팅을 다시 읽어 최종 응답을 확인한다.
  관련 assistant 메시지가 완료 상태가 아니면 완료하지 않는다.

현재 검증기는 프로젝트의 `items/cursor`, 대화의 `mapping/page_info` 구조를 처리한다.
실제 계정의 응답이 이 구조와 다르면 안전하게 중단되며, 원본 진단 파일 또는
로그인된 Chrome에서 해당 구조를 추가로 확인해야 한다.

## 재시도·보존·재개

목록 읽기, 채팅 읽기, 파일 저장은 해당 단계에서 최대 3회 시도한다. 저장 재시도는
검증된 snapshot을 그대로 사용하므로 다음 채팅으로 이동하지 않는다. 복구되지 않으면
전체 업데이트를 `paused`로 반환하며 `update-progress.json`에 현재 단계, 발견한
채팅 목록, 미완료 채팅을 기록한다. 다시 같은 명령을 실행하면 프로젝트 목록을
검증한 뒤 미완료 채팅부터 처리한다. 이전 완료 채팅도 다시 검증한다.

기존 MD 바이트를 유지하고 변경된 대화의 전체 버전을 추가한다. 동일한 QA 개수의
내용 변경과 개수 감소도 보존한다. 길이와 SHA-256으로 버전을 검증하며, 임시 파일
검증·원자 교체·최종 파일 재읽기까지 성공해야 `project.json`에 완료를 기록한다.
상태 저장만 중단된 경우에는 파일 내용을 실제 snapshot과 비교한 후 중복 없이 복구한다.
기존 MD의 표시 없는 꼬리 내용도 삭제하지 않는다.

QA로 표현되지 않는 마지막 미응답 사용자 메시지와 빈 대화도 저장한다.
`qa_count`는 최신 버전의 개수이며, 파일 전체의 과거 Q/A 제목 합계가 아니다.
전체 버전과 원문을 보존하므로 파일 용량은 기존 방식보다 커질 수 있다.

## 테스트 결과

| 구분 | 결과 | 검증 범위 |
| --- | --- | --- |
| Python CLI 모의 테스트 | 79 통과, 선택 실행 2 제외 | CDP 이벤트, cursor, 가상화, 지연, 생성, 내용·첨부 검증, 기존 기능 |
| Python controller 모의 테스트 | 59 통과 | 단계 재시도, 중단·재개, 상태 기록, 내용 변경, 저장 실패, 기존 기능 |
| Node 확장 테스트 | 20 통과 | 기존 Markdown 변환, 상태 전이, 저장 큐 |
| 실제 Chrome + 합성 응답 | 1 통과 | 실제 CDP 본문 수집, 지연된 과거 페이지, 숨겨진 system cursor, DOM 가상화 |
| 실제 로그인 계정 | 차단 | CDP 연결 성공, Google 로그인 화면으로 이동하여 전체 보존 미검증 |

**모의 테스트 158개 통과**와 **실제 Chrome 합성 통합 테스트 1개 통과**는 실제
계정의 모든 채팅을 보존했다는 의미가 아니다. 정적 검사와 `git diff --check`도 통과했다.
기계 판독 기록: [preservation-validation.json](preservation-validation.json).

다음 명령은 저장소 루트의 PowerShell에서 실행한다.

```powershell
$env:PYTHONPATH = "$PWD/CLI-gpt;$PWD/controller"
python -m unittest discover -s CLI-gpt/tests -q
python -m unittest discover -s controller/tests -q
node --test extension/tests/*.test.js

# 실제 Chrome을 사용하는 합성 통합 테스트
$env:OUTOGPT_CDP_FIXTURE = '1'
python -m unittest discover -s CLI-gpt/tests/integration -p test_archive_cdp_fixture.py -v
Remove-Item Env:OUTOGPT_CDP_FIXTURE

# 기존 setup으로 로그인한 뒤 읽기 전용 응답 구조 진단
python CLI-gpt/tests/integration/probe_live_archive.py
```

## setup 유지 확인

setup 구현, Chrome 실행·프로필 설정, 저장 경로 해석, 확장 setup, 기존 Markdown
변환기를 기준 커밋과 비교했다. 모두 유지된다. controller CLI 변경은 프로젝트
업데이트 결과를 `paused` 및 `pending_chat_id`로 출력하는 부분에만 있다.
환경의 기존 `.venv` 실행 파일이 동작하지 않아 테스트는 설치된 Python 3.12로
실행했다. 이를 위해 setup이나 기존 가상환경을 재설정하지 않았다.
