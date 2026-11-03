# 정보입력 화면 (information)

회원가입 3단계 **정보입력** 화면입니다. `index.html` 파일 하나에 HTML·CSS·JS가 모두 들어 있어서 브라우저로 바로 열면 됩니다.
디자인 값은 저장소의 `gs-shop.md`(GS SHOP 디자인 시스템)를 따랐습니다.

## 기존 신세계면세점 화면에서 바꾼 점

| 항목 | 기존 | 변경 |
| --- | --- | --- |
| 비밀번호 안내 | 입력창 안 placeholder 2줄 | placeholder는 "비밀번호 입력" 한 줄, 규칙은 **입력창 아래**에 체크리스트로 꺼냄 (조건을 만족하면 초록 체크) |
| 비밀번호 확인 | 같은 값을 쳐도 "일치하지 않음"이 뜸 | 같으면 절대 오류가 뜨지 않고 "비밀번호가 일치해요" 표시. 아직 입력 중일 때도 오류를 띄우지 않음 |
| 이메일 | placeholder "이메일 아이디(이메일 계정)" 2줄 + 도메인 선택이 다음 줄 | "이메일 아이디" 한 줄, `아이디 @ 도메인`을 한 줄로. `abc@naver.com`을 통째로 붙여넣어도 자동으로 나눔 |
| 자택주소 | 가입 단계에서 필수 입력 | **항목 삭제** (가입에 꼭 필요한 정보만 받기) |

그 밖에: 아이디는 대문자 자동 소문자 변환 + 중복확인 후 값을 바꾸면 다시 확인 필요, 비밀번호 보기 버튼, 모든 필수값이 채워져야 "다음" 버튼 활성화.

## 다른 화면과 연결하는 방법

### 1. 앞 단계(본인인증)에서 이름·휴대폰번호 넘겨주기

셋 중 아무 방법이나 쓰면 됩니다.

```js
// (A) sessionStorage에 넣고 이 페이지로 이동
sessionStorage.setItem('shin_verified', JSON.stringify({ name: '김형민', phone: '01050114360' }));
location.href = 'information/index.html';

// (B) 주소 뒤에 붙이기
// information/index.html?name=김형민&phone=01050114360

// (C) 같은 페이지 안에 있으면 직접 호출
ShinInfo.setVerified({ name: '김형민', phone: '01050114360' });
```

아무것도 안 넘기면 화면 확인용 예시 값(김신세 / 010-1234-5678)이 들어갑니다.

### 2. 다음 단계(약관 동의 등)에서 입력값 받기

"다음"을 누르면 세 가지가 동시에 일어납니다.

```js
// (A) 이벤트 — 같은 페이지에 합칠 때
document.addEventListener('shin:info-complete', (e) => {
  console.log(e.detail); // 아래 데이터 형식 참고 (비밀번호 포함)
});

// (B) sessionStorage — 다른 페이지로 넘어갈 때 (비밀번호는 보안상 저장하지 않음)
JSON.parse(sessionStorage.getItem('shin_signup_info'));

// (C) iframe으로 넣었을 때 부모 창에 postMessage (type: 'shin:info-complete')
```

다음 페이지로 자동 이동하려면 `index.html` 안의 `config.nextUrl`에 주소를 넣으세요. (예: `'../agreement/index.html'`)

데이터 형식:

```json
{
  "userId": "gudals4361",
  "password": "Abcd1234!",
  "name": "김형민",
  "phone": "010-5011-4360",
  "email": "gudals4340@naver.com"
}
```

### 3. 아이디 중복확인을 서버와 연결

지금은 `admin`, `shinsegae`, `test1234`, `gudals4360`만 "사용 중"으로 나오는 예시입니다. 서버가 생기면 `config.checkIdAvailable` 함수만 바꾸면 됩니다.

```js
ShinInfo.config.checkIdAvailable = (id) =>
  fetch('/api/check-id?id=' + id).then(r => r.json()).then(d => d.available);
```

### 4. 스타일 충돌

클래스 이름과 CSS 변수는 모두 `si-` 접두사를 붙여서, 다른 사람 CSS와 합쳐도 겹치지 않습니다.
