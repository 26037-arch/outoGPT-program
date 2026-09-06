# outoGPT Chat Markdown Extension

> In the normal OutoGPT path, do not load this extension into a personal Chrome
> profile. Initial manual authentication runs without any extension. After that
> browser closes, OutoGPT restarts Playwright Chromium with the same dedicated
> profile, loads this extension, and verifies its Manifest V3 service worker.

## 목적과 현재 범위

Manifest V3 Chrome Extension으로, 등록한 ChatGPT Project 한 개의 conversation을 사용자가 선택한 폴더에 Markdown으로 자동 보관합니다. 이 확장은 `outoGPT-program`의 브라우저 어댑터 계층이며, 프롬프트 입력·workflow graph·LLM 판단·스케줄링은 구현하지 않습니다.

현재 지원 범위:

- File System Access API로 임의의 루트 폴더 선택
- ChatGPT Project URL 한 개 등록
- 실제 프로젝트 페이지 DOM에서 프로젝트 이름 추출
- 등록 프로젝트의 여러 탭을 각각 독립적으로 관찰
- user message 직후, streaming debounce 시점, response 완료 직후 전체 conversation 재저장
- YAML frontmatter와 User/Assistant 순서를 가진 Markdown 생성
- conversation ID 기반 파일 매핑 및 제목 충돌 suffix
- 브라우저 재시작 후 설정과 directory handle 복원

## Chrome에 로드

1. Chrome에서 `chrome://extensions`를 엽니다.
2. **개발자 모드**를 켭니다.
3. **압축해제된 확장 프로그램을 로드합니다**를 선택합니다.
4. 이 `extension/` 폴더를 선택합니다.
5. 툴바의 확장 프로그램 아이콘을 누르거나 확장 상세 화면에서 옵션을 엽니다.

## 최초 설정

1. **루트 폴더 선택**을 눌러 Markdown 저장 위치를 선택합니다.
2. 로그인 가능한 `https://chatgpt.com/...` Project URL을 붙여 넣습니다.
3. **프로젝트 감지 및 등록**을 누릅니다.
4. 확장이 해당 ChatGPT 탭을 열고 안정적으로 세 번 관찰된 프로젝트 이름을 등록합니다.
5. `<root>/ChatGPT/<project-name>/`이 만들어집니다.

프로젝트 이름을 URL slug에서 만들거나 사용자에게 입력받지 않습니다. 이름 추출에 실패하면 `PROJECT_NAME_EXTRACTION_ERROR`를 표시하고 등록하지 않습니다.
대화 URL을 Project URL로 잘못 붙여 넣으면 등록 단계에서 거부합니다.

## 저장 폴더와 Markdown

```text
<selected-root>/
└── ChatGPT/
    └── <sanitized-project-name>/
        └── <sanitized-conversation-title>.md
```

동일 제목이 이미 다른 conversation에 할당되어 있으면 ` -- <conversation-id 6자리>` suffix를 붙입니다. 파일명 매핑은 제목이 아니라 `conversationId`를 key로 저장합니다.

```md
---
conversation_id: "..."
project_id: "..."
project_name: "..."
title: "..."
url: "https://chatgpt.com/..."
updated_at: "2026-09-06T00:00:00.000Z"
---

# Conversation title

## User

질문

## Assistant

답변
```

DOM을 단순 `innerText`로 저장하지 않습니다. paragraph, heading, emphasis, nested list, blockquote, inline/fenced code, language, table, link, 이미지와 KaTeX의 원본 TeX annotation을 의미에 맞게 Markdown으로 변환합니다.

## 응답 완료 감지

각 탭의 `ChatAgent`가 독립적인 `ResponseStateMachine`을 갖습니다.

```text
IDLE
  → user message 추가
WAITING_FOR_START
  → Stop control 존재 2회 연속(100ms polling)
GENERATING
  → Stop control 없음
WAITING_FOR_END
  → Stop control 없음 5회 연속 + assistant DOM 750ms 이상 안정
COMPLETE
```

Stop control이 다시 나타나면 `GENERATING`으로 돌아갑니다. 생성 시작 30초 timeout과 전체 응답 600초 timeout은 `COMPLETE`가 아니라 각각 `START_TIMEOUT`, `RESPONSE_TIMEOUT` 오류가 됩니다.

현재 selector 후보는 [content/selectors.js](content/selectors.js)에만 모여 있습니다. 우선순위는 `data-testid`, 정확한 `aria-label`, 접근 가능한 버튼 이름이며 CSS class 하나만으로 판정하지 않습니다. selector 진단과 상태 전이는 탭 DevTools console에서 `[outoGPT:ChatAgent:<tabId>]` prefix로 확인할 수 있습니다.

## 권한과 복원

- `storage`: 프로젝트 설정, conversation 파일 매핑, 마지막 오류/UI 상태 저장
- `https://chatgpt.com/*`: 필요한 ChatGPT 페이지만 content script로 관찰
- File System Access: setup page의 사용자 gesture로만 폴더 선택/권한 요청

`FileSystemDirectoryHandle`은 JSON으로 직렬화하지 않고 extension origin의 IndexedDB에 저장합니다. service worker는 저장 전에 `queryPermission({mode: "readwrite"})`만 수행합니다. 권한이 `prompt` 또는 `denied`이면 자동 승인하지 않고 설정 화면에 `FILESYSTEM_PERMISSION_ERROR`를 남깁니다. 사용자가 설정 화면의 **폴더 권한 재승인** 버튼으로 처리해야 합니다.

## 내부 구조와 controller 경계

```text
future outoGPT controller
          │ GET_STATE / SAVE_MARKDOWN
          ▼
background/router.js
          │ tabId로 route
    ┌─────┼─────┐
    ▼     ▼     ▼
 Agent A Agent B Agent C
          │
          ▼
conversation extractor → Markdown converter → serialized filesystem writer
```

`shared/messages.js`에 `CHAT_STATE`, `RESPONSE_STARTED`, `RESPONSE_COMPLETED`, `SAVE_CONVERSATION`, `SAVE_COMPLETED` protocol이 정의되어 있습니다. `SEND_PROMPT`, `GET_STATE`, `SAVE_MARKDOWN`, `EXPORT_MARKDOWN` 이름도 향후 controller 경계로 예약했지만, 이번 버전에서 prompt 전송은 의도적으로 구현하지 않았습니다.

## 테스트

Node.js 20 이상에서 외부 패키지 없이 실행합니다.

```bash
cd extension
node --test tests/*.test.js
```

테스트는 상태 전이, Stop flicker/재등장, 시작·응답 timeout, 새 response, 파일명 sanitize, project/conversation ID, Markdown 구조와 latest-save queue를 검증합니다.

## 알려진 한계와 수동 검증

- ChatGPT DOM은 공식 계약이 아니므로 UI 변경 시 `content/selectors.js`, `content/project-detector.js`, `markdown/chatgpt-rules.js`를 수정해야 할 수 있습니다.
- Project name과 conversation title selector는 계정별 UI/언어/A-B test의 영향을 받을 수 있습니다. 임의 fallback 이름 대신 등록 오류를 우선합니다.
- File System Access permission의 재승인은 background service worker가 할 수 없으므로 설정 화면을 열어야 합니다.
- 동일 프로젝트 판정은 URL path의 `g-p-...` segment, conversation 판정은 `/c/<id>` segment를 사용합니다.
- 현재 구현은 프로젝트 대화 URL에도 `g-p-...` segment가 남는 URL 형태를 전제로 합니다. 계정별 UI에서 대화 URL이 `/c/<id>`만 노출된다면 프로젝트 소속을 확인할 별도 DOM marker 규칙을 추가해야 합니다.
- Chrome에서 unpacked extension을 실제 로드한 뒤 프로젝트 이름, 현재 Stop control의 accessible name, 모든 메시지 role DOM, KaTeX/표/코드 블록 변환과 재시작 후 폴더 권한을 수동 검증해야 합니다.
