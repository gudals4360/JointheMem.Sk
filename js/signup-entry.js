// 통합회원 가입만 다음 화면으로 이동. 나머지는 프로토타입 안내.
document.querySelectorAll("[data-method]").forEach((btn) =>
  btn.addEventListener("click", () =>
    showToast(`${btn.dataset.method}은 프로토타입에서 준비 중이에요.`)
  )
);
