# CLI-GPT

ChatGPT 웹 UI에 Playwright로 프롬프트를 전달하는 최소 어댑터입니다. OpenAI API나 ChatGPT 비공개 API를 호출하지 않으며, 답변 본문을 저장하거나 분석하지 않습니다.

핵심 Python API는 다음 두 함수입니다.

```python
from cli_gpt.chatgpt import create_chat, continue_chat

chat_url = create_chat(PROJECT_URL, "초기 분석을 수행하라.")
continue_chat(chat_url, "방금 분석의 문제점을 검토하라.")
```

## 설치

Python 3.11 이상이 필요합니다.

```bash
pip install -e .
playwright install chromium
```

## 최초 설정

```bash
gpt setup "<PROJECT_URL>"
```

설정은 `data/config.json`에 저장되며 Git에서 제외됩니다.

## 최초 로그인

브라우저는 디버깅과 수동 로그인을 위해 항상 headed mode로 열립니다. 처음 `gpt new` 또는 `gpt send`를 실행했을 때 로그인 화면이나 CAPTCHA가 보이면 열린 브라우저에서 직접 처리하십시오. 프로그램은 최대 5분 동안 prompt 입력창이 나타나기를 기다립니다.

이메일, 비밀번호, 쿠키를 설정 파일에 기록하지 않습니다. 로그인 세션은 Playwright persistent profile인 `data/browser-profile/`에 브라우저 데이터 형태로 보관되어 다음 실행에서 재사용됩니다. 이 디렉터리는 민감한 세션 정보를 포함할 수 있으므로 공유하거나 버전 관리하지 마십시오.

## 새 채팅

설정한 프로젝트 안에서 새 conversation을 생성합니다.

```bash
gpt new "아이디어를 분석해줘"
```

## 기존 채팅

ChatGPT conversation URL 자체를 식별자로 사용합니다.

```bash
gpt send "https://chatgpt.com/c/..." "문제점을 더 분석해줘"
```

성공 시 마지막 두 줄은 파싱하기 쉬운 형식입니다.

```text
status: completed
chat_url: https://chatgpt.com/c/...
```

## 동작 방식

- `data/browser.lock`을 원자적으로 획득하여 같은 browser profile의 동시 사용을 막습니다. 종료된 프로세스나 24시간을 넘긴 lock은 stale lock으로 정리합니다.
- prompt 입력창은 accessible role, ARIA, test ID, `contenteditable`, `textarea` 순의 fallback으로 찾고 visible/editable/enabled 상태를 확인합니다.
- prompt 전체를 Playwright로 먼저 입력한 뒤 Send 버튼을 클릭하며, 버튼을 찾지 못하면 마지막에 Enter를 한 번만 누릅니다.
- 완료 감지는 stop-generation control, assistant message 수/내용, prompt composer와 Send 버튼 복귀 상태를 함께 관찰합니다.
- 생성 시작 기본 timeout은 30초, 전체 생성 timeout은 600초입니다.

## 테스트

Playwright나 실제 ChatGPT 접속 없이 unit test를 실행할 수 있습니다.

```bash
python -m unittest discover -v
```

실제 브라우저 integration test는 새 conversation을 생성하므로 기본 실행에서 제외됩니다. 명시적으로 실행하려면 PowerShell에서 다음 환경 변수를 설정합니다.

```powershell
$env:CLI_GPT_RUN_INTEGRATION = "1"
$env:CLI_GPT_PROJECT_URL = "https://chatgpt.com/..."
python -m unittest tests.integration.test_live_chatgpt -v
```

## 제약

ChatGPT 웹 UI는 공식 자동화 API 계약이 아니므로 DOM 변경에 따라 `cli_gpt/selectors.py`의 selector를 수정해야 할 수 있습니다. 특히 Send/Stop 버튼의 접근성 이름, assistant message의 `data-message-author-role`, 프로젝트 페이지의 새 채팅 control이 UI 변경에 가장 민감합니다.

로그인, CAPTCHA, 인증을 자동 입력하거나 우회하지 않습니다. 답변 내용 parsing, Markdown export, conversation DB, scheduler, agent loop도 이 버전의 범위에 포함되지 않습니다.

